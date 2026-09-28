"""Документы взаимодействия: файлы проекта, привязанные к стадии, с версиями.

стадию задаёт вызывающий - к любой стадии воркфлоу этого взаимодействия, не
только к текущей. Что на каком шаге обязательно, решает слой выше, здесь -
только структура. Новая версия ссылается на прежнюю, прежняя остаётся -
«уходит вниз и сереет». Удаляет только админ - единственное исключение из
запрета доменных операций
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from quoll.attachments.models import Attachment
from quoll.attachments.service import AttachmentService
from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.auth.models import User, UserRole
from quoll.catalog.models import DocumentKind
from quoll.core.exceptions import (
    DomainRuleException,
    IdNotExistsException,
    OperationForbiddenException,
)
from quoll.interactions import contract_service
from quoll.interactions.access_policy import can_change, can_close, can_read
from quoll.interactions.bindings import check_contract_dates
from quoll.interactions.models import (
    AgreementStatus,
    Branch,
    DocumentStatus,
    Interaction,
    InteractionDocument,
    SupplementaryAgreement,
)
from quoll.interactions.notify import notify
from quoll.interactions.repository import InteractionRepository
from quoll.interactions.scope import lock_interaction_scope
from quoll.notifications import kinds
from quoll.notifications.kinds import Subject
from quoll.workflows.models import Stage, TransitionAttachment

# вид ставится сам на своих кнопках и не живёт на шагах веток (Д5)
CONTRACT_KINDS = frozenset({"CONTRACT", "SUPPLEMENTARY_AGREEMENT"})
# доп. прохождение не меняет реквизиты договора напрямую (Д48)
CONTRACT_DOCUMENT_KINDS = frozenset({"CONTRACT", "CONTRACT_DRAFT"})


@dataclass(frozen=True)
class DocumentFields:
    """что пользователь говорит о файле; пустое у новой версии берётся
    у прежней"""

    kind: str | None = None
    title: str | None = None
    description: str | None = None
    contract_number: str | None = None
    contract_signed_at: date | None = None
    contract_valid_until: date | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentView:
    document: InteractionDocument
    attachment: Attachment
    is_current: bool
    # действующая преемница: где обновлён файл (Д7)
    replaced_by: InteractionDocument | None = None


async def upload(
    session: AsyncSession,
    attachments: AttachmentService,
    *,
    interaction_id: int,
    actor: User,
    file: UploadFile,
    stage_id: int,
    replaces_document_id: int | None,
    fields: DocumentFields,
    branch_id: int | None = None,
    supplementary_agreement_id: int | None = None,
    side_pointer_id: int | None = None,
) -> DocumentView:
    """право - дважды: без блокировок до загрузки, чтобы не держать строки на
    время сети, и под блокировкой перед вставкой"""
    repo = InteractionRepository(session)
    interaction = await repo.get(interaction_id)
    if not can_change(actor, await repo.ownership(interaction)):
        raise OperationForbiddenException("attach documents to this interaction")
    await _check_stage(session, interaction.workflow_id, stage_id, branch_id)

    attachment = await attachments.upload_attachment(file)
    try:
        scope = await lock_interaction_scope(session, interaction_id, actor.id)
        if not can_change(scope.actor, scope.ownership):
            raise OperationForbiddenException("attach documents to this interaction")
        pointer = None
        if side_pointer_id is not None:
            pointer = await contract_service.active_pass(
                session, interaction_id, side_pointer_id
            )
            contract_service.check_side_target(
                await session.get(Stage, stage_id), branch_id
            )
        previous = None
        if replaces_document_id is not None:
            previous = await _check_replaceable(
                session, interaction_id, replaces_document_id
            )
            if previous.side_pointer_id != side_pointer_id:
                raise DomainRuleException(400, "New version stays in its pass")
            # версия живёт на стадии прежней или на её подшаге (Д6) - иначе
            # замена из текущего шага обошла бы аппрув правки пройденного
            stage = await session.get(Stage, stage_id)
            if previous.branch_id != branch_id or previous.stage_id not in {
                stage_id,
                stage.parent_stage_id,
            }:
                raise DomainRuleException(
                    400,
                    "New version goes to the stage of the replaced one or its sub-step",
                )
        await _check_agreement_scan(
            session, scope.interaction, stage_id, previous, supplementary_agreement_id
        )
        values = await _document_values(
            session,
            fields,
            previous,
            branch_id,
            for_agreement=supplementary_agreement_id is not None,
        )
        # вид мог прийти и от заменяемой версии
        if pointer is not None and values.kind in CONTRACT_DOCUMENT_KINDS:
            raise DomainRuleException(
                400, "Contract changes only through agreement actions"
            )
        current = scope.interaction.state_id
        if branch_id is not None:
            branch = await session.get(Branch, branch_id)
            if branch is None or branch.interaction_id != interaction_id:
                raise DomainRuleException(404, "Branch is not in this interaction")
            if branch.state_id is None:
                raise DomainRuleException(
                    409, "Branch is a draft until the contract is signed"
                )
            current = branch.state_id
        # правка файла пройденного шага - с аппрувом руководителя (AS IS);
        # скан допсоглашения одобряют вместе с ним. У доп. прохождения
        # пройден любой его шаг, кроме текущего
        if pointer is not None:
            passed = stage_id != pointer.stage_id
        else:
            passed = contract_service.step_passed(
                scope.interaction, stage_id, current, branch_id
            )
        pending = (
            supplementary_agreement_id is None
            and actor.role == UserRole.MANAGER
            and passed
        )
        if not pending and values.kind == "CONTRACT":
            values = await _keep_extended_term(session, interaction_id, values)
        document = InteractionDocument(
            status=DocumentStatus.PENDING if pending else DocumentStatus.ACTIVE,
            interaction_id=interaction_id,
            attachment_id=attachment.id,
            stage_id=stage_id,
            branch_id=branch_id,
            uploaded_by=actor.id,
            replaces_document_id=replaces_document_id,
            title=values.title or attachment.filename,
            kind=values.kind,
            description=values.description,
            contract_number=values.contract_number,
            contract_signed_at=values.contract_signed_at,
            contract_valid_until=values.contract_valid_until,
            supplementary_agreement_id=supplementary_agreement_id,
            side_pointer_id=side_pointer_id,
            meta=values.meta,
        )
        session.add(document)
        await session.flush()
    except Exception:
        # осиротевший файл безвреден, но и держать его незачем
        await attachments.s3.delete(attachment.storage_key)
        raise

    if pending:
        await notify(
            session,
            kinds.DOCUMENT_PENDING,
            scope,
            context={"document": document.title},
            subject=Subject.DOCUMENT,
            subject_id=document.id,
            payload={"document_id": document.id},
        )
    record(
        session,
        actor_id=actor.id,
        event_type=AuditEventType.DOCUMENT_ATTACHED,
        target_type=TargetType.DOCUMENT,
        target_id=document.id,
        new_value={
            "interaction_id": interaction_id,
            "stage_id": stage_id,
            "title": document.title,
            "kind": document.kind,
            "filename": attachment.filename,
            "replaces_document_id": replaces_document_id,
        },
    )
    await session.refresh(document)
    return DocumentView(document, attachment, is_current=not pending)


async def _check_agreement_scan(
    session: AsyncSession,
    interaction: Interaction,
    stage_id: int,
    previous: InteractionDocument | None,
    sa_id: int | None,
) -> None:
    """скан допсоглашения меняется только через него и только в черновике;
    обычная загрузка на 4.1 - только после подписания"""
    if sa_id is not None:
        sa = await session.get(SupplementaryAgreement, sa_id, populate_existing=True)
        if sa is None or sa.interaction_id != interaction.id:
            raise DomainRuleException(
                404, "Supplementary agreement is not in this interaction"
            )
        if sa.status != AgreementStatus.DRAFT:
            raise DomainRuleException(
                409, f"Supplementary agreement is {sa.status}, only a draft is changed"
            )
        if previous is not None and previous.supplementary_agreement_id != sa_id:
            raise DomainRuleException(400, "Replace a scan of this agreement")
        if previous is None and await current_scan(session, sa_id) is not None:
            raise DomainRuleException(409, "Agreement already has a scan, replace it")
        return
    if previous is not None and previous.supplementary_agreement_id is not None:
        raise DomainRuleException(
            400, "Supplementary agreement scan is replaced through the agreement"
        )


async def _keep_extended_term(session: AsyncSession, interaction_id: int, doc):
    """П10: версия договора, становясь текущей, не откатывает срок, продлённый
    допсоглашением. doc - DocumentFields или сам документ"""
    from dataclasses import replace

    from quoll.interactions.bindings import current_contract
    from quoll.interactions.models import InteractionStageHistory, StageChangeKind

    extended = await session.scalar(
        select(InteractionStageHistory.id)
        .where(
            InteractionStageHistory.interaction_id == interaction_id,
            InteractionStageHistory.kind == StageChangeKind.CONTRACT_EXTENDED,
        )
        .limit(1)
    )
    current = await current_contract(session, interaction_id) if extended else None
    if current is None or current.contract_valid_until is None:
        return doc
    if doc.contract_valid_until is not None and (
        doc.contract_valid_until >= current.contract_valid_until
    ):
        return doc
    if isinstance(doc, DocumentFields):
        return replace(doc, contract_valid_until=current.contract_valid_until)
    doc.contract_valid_until = current.contract_valid_until
    return doc


async def current_scan(session: AsyncSession, sa_id: int) -> InteractionDocument | None:
    """действующий скан допсоглашения: ACTIVE и без действующей преемницы"""
    return await session.scalar(
        select(InteractionDocument)
        .where(
            InteractionDocument.supplementary_agreement_id == sa_id,
            InteractionDocument.status == DocumentStatus.ACTIVE,
            ~replaced_expression(),
        )
        .order_by(InteractionDocument.id.desc())
        .limit(1)
    )


async def _document_values(
    session: AsyncSession,
    fields: DocumentFields,
    previous: InteractionDocument | None,
    branch_id: int | None,
    *,
    for_agreement: bool = False,
) -> DocumentFields:
    """вид из справочника; «другое» - с описанием; реквизиты - только у
    договора, и номер у него обязателен"""
    if previous is not None:
        inherited = {
            name: getattr(previous, name)
            for name in DocumentFields.__dataclass_fields__
            if name != "meta"
        }
        fields = DocumentFields(
            **{name: getattr(fields, name) or inherited[name] for name in inherited},
            meta=fields.meta,
        )
    if fields.kind is None:
        raise DomainRuleException(422, "Document kind is required")
    if not await session.scalar(
        select(exists().where(DocumentKind.code == fields.kind))
    ):
        raise DomainRuleException(400, f"Unknown document kind '{fields.kind}'")
    if fields.kind == "OTHER" and not fields.description:
        raise DomainRuleException(422, "Describe a document of kind OTHER")
    if fields.kind == "SUPPLEMENTARY_AGREEMENT" and not for_agreement:
        raise DomainRuleException(
            400, "Supplementary agreement scan is uploaded through the agreement"
        )
    if fields.kind in CONTRACT_KINDS and branch_id is not None:
        raise DomainRuleException(
            400, "Contracts and supplementary agreements are not branch files"
        )
    has_contract_fields = any(
        (fields.contract_number, fields.contract_signed_at, fields.contract_valid_until)
    )
    if fields.kind != "CONTRACT" and has_contract_fields:
        raise DomainRuleException(400, "Contract details belong to a contract")
    check_contract_dates(fields.contract_signed_at, fields.contract_valid_until)
    return fields


async def _check_stage(
    session: AsyncSession, workflow_id: int | None, stage_id: int, branch_id: int | None
) -> None:
    """только структура: стадия из воркфлоу этого взаимодействия и живая"""
    if workflow_id is None:
        raise DomainRuleException(
            409, "Interaction has no workflow yet, no stages to attach to"
        )
    stage = await session.get(Stage, stage_id)
    if stage is None or stage.workflow_id != workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    if stage.archived_at is not None:
        raise DomainRuleException(409, f"Stage '{stage_id}' is archived")
    if stage.is_branch_stage != (branch_id is not None):
        raise DomainRuleException(400, "Files of branch stages belong to a branch")


async def _check_replaceable(
    session: AsyncSession, interaction_id: int, document_id: int
) -> InteractionDocument:
    previous = await session.get(InteractionDocument, document_id)
    if previous is None or previous.interaction_id != interaction_id:
        raise DomainRuleException(
            409, "Replaced document belongs to another interaction"
        )
    successor = await session.scalar(
        select(InteractionDocument.id).where(
            InteractionDocument.replaces_document_id == document_id
        )
    )
    if successor is not None:
        raise DomainRuleException(
            409, f"Document is already replaced by '{successor}', replace that one"
        )
    return previous


def replaced_expression():
    """версию сменила одобренная преемница - ждущая ещё не сменила"""
    successor = aliased(InteractionDocument)
    return exists().where(
        successor.replaces_document_id == InteractionDocument.id,
        successor.status == DocumentStatus.ACTIVE,
    )


async def documents(session: AsyncSession, interaction_id: int) -> list[DocumentView]:
    successor = aliased(InteractionDocument)
    rows = await session.execute(
        select(InteractionDocument, Attachment, successor)
        .join(Attachment, Attachment.id == InteractionDocument.attachment_id)
        .outerjoin(
            successor,
            (successor.replaces_document_id == InteractionDocument.id)
            & (successor.status == DocumentStatus.ACTIVE),
        )
        .where(InteractionDocument.interaction_id == interaction_id)
        .order_by(InteractionDocument.created_at, InteractionDocument.id)
    )
    return [
        DocumentView(
            doc,
            attachment,
            is_current=doc.status == DocumentStatus.ACTIVE and newer is None,
            replaced_by=newer,
        )
        for doc, attachment, newer in rows
    ]


async def delete_document(
    session: AsyncSession, *, document_id: int, actor_id: str
) -> str:
    """удаляется вложение, документ уходит каскадом. Возвращает ключ файла:
    из хранилища его убирают после коммита - строка без файла хуже, чем файл
    без строки"""
    found = await session.get(InteractionDocument, document_id)
    if found is None:
        raise IdNotExistsException(InteractionDocument.__name__)
    # под захватом области: иначе параллельная замена раздвоила бы цепочку
    await lock_interaction_scope(session, found.interaction_id, actor_id)
    document = await session.get(
        InteractionDocument, document_id, populate_existing=True
    )
    if document is None:
        # удалили параллельно, пока ждали блокировку
        raise IdNotExistsException(InteractionDocument.__name__)
    attachment = await session.get(Attachment, document.attachment_id)
    successor = await session.scalar(
        select(InteractionDocument).where(
            InteractionDocument.replaces_document_id == document_id
        )
    )
    predecessor_id = document.replaces_document_id

    # сначала удалить, потом перевесить: иначе преемник и удаляемый на миг
    # заменяли бы одного предшественника, а индекс это запрещает
    await session.execute(delete(Attachment).where(Attachment.id == attachment.id))
    session.expunge(document)
    if successor is not None:
        await session.refresh(successor)
        successor.replaces_document_id = predecessor_id
    await session.flush()

    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.DOCUMENT_DELETED,
        target_type=TargetType.DOCUMENT,
        target_id=document_id,
        old_value={
            "interaction_id": document.interaction_id,
            "filename": attachment.filename,
            "replaces_document_id": predecessor_id,
        },
    )
    return attachment.storage_key


async def decide(
    session: AsyncSession,
    *,
    document_id: int,
    actor_id: str,
    approve: bool,
    comment: str | None,
) -> DocumentView:
    """руководитель владельца одобряет или отклоняет файл на пройденный шаг"""
    found = await session.get(InteractionDocument, document_id)
    if found is None:
        raise IdNotExistsException(InteractionDocument.__name__)
    scope = await lock_interaction_scope(session, found.interaction_id, actor_id)
    if not can_close(scope.actor, scope.ownership):
        raise OperationForbiddenException("decide on this document")
    document = await session.get(
        InteractionDocument, document_id, populate_existing=True
    )
    if document.status != DocumentStatus.PENDING:
        raise DomainRuleException(409, f"Document is already {document.status}")
    if document.side_pointer_id is not None:
        await contract_service.active_pass(
            session, document.interaction_id, document.side_pointer_id
        )
    if approve:
        if document.kind == "CONTRACT":
            await _keep_extended_term(session, document.interaction_id, document)
        document.status = DocumentStatus.ACTIVE
    else:
        document.status = DocumentStatus.REJECTED
        # отклонённая выходит из цепочки: иначе заняла бы место преемницы
        document.replaces_document_id = None
    await session.flush()
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.DOCUMENT_APPROVED
        if approve
        else AuditEventType.DOCUMENT_REJECTED,
        target_type=TargetType.DOCUMENT,
        target_id=document.id,
        new_value={"comment": comment},
    )
    await notify(
        session,
        kinds.DOCUMENT_DECIDED,
        scope,
        context={
            "decision": "одобрен" if approve else "отклонён",
            "document": document.title,
            "comment": comment or "",
        },
        subject=Subject.DOCUMENT,
        subject_id=document.id,
        payload={"document_id": document.id},
        editor_id=document.uploaded_by,
    )
    attachment = await session.get(Attachment, document.attachment_id)
    await session.refresh(document)
    return DocumentView(document, attachment, is_current=approve)


async def check_attachment_readable(
    session: AsyncSession, user: User, attachment_id: int
) -> None:
    """файл заявки - по её политике чтения, шаблон ребра - любому вошедшему,
    ни к чему не привязанный - только админу: иначе он стал бы «шаблоном» для
    всех, как только от него отвязали документ"""
    if await session.get(Attachment, attachment_id) is None:
        raise IdNotExistsException(Attachment.__name__)
    document = await session.scalar(
        select(InteractionDocument).where(
            InteractionDocument.attachment_id == attachment_id
        )
    )
    if document is not None:
        repo = InteractionRepository(session)
        interaction = await repo.get(document.interaction_id)
        if not can_read(user, await repo.ownership(interaction)):
            raise OperationForbiddenException("read this file")
        return
    template = await session.scalar(
        select(exists().where(TransitionAttachment.attachment_id == attachment_id))
    )
    if not template and user.role != UserRole.ADMIN:
        raise OperationForbiddenException("read this file")


async def is_interaction_document(session: AsyncSession, attachment_id: int) -> bool:
    return bool(
        await session.scalar(
            select(exists().where(InteractionDocument.attachment_id == attachment_id))
        )
    )
