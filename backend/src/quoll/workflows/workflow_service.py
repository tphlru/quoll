"""Правка графа воркфлоу. Всё - админ, под блокировкой строки воркфлоу,
в журнал. Опубликованный граф после правки обязан остаться проходимым"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.core.exceptions import (
    DomainRuleException,
    IdNotExistsException,
    PublishedGraphChangeException,
)
from quoll.core.locking import lock_row
from quoll.workflows.graph_policy import StageFacts, edge_facts, graph_problems
from quoll.workflows.models import Stage, Workflow, WorkflowTransition
from quoll.workflows.schemas import (
    StageCreate,
    StageUpdate,
    WorkflowCreate,
    WorkflowTransitionCreate,
    WorkflowTransitionUpdate,
    WorkflowUpdate,
)

# концы ребра - то, на что ссылается история переходов заявок
_EDGE_ENDS = frozenset({"from_stage_id", "to_stage_id"})


async def lock_workflow(session: AsyncSession, workflow_id: int) -> Workflow:
    workflow = await lock_row(session, Workflow, workflow_id)
    if workflow is None:
        raise IdNotExistsException(Workflow.__name__)
    return workflow


async def check_graph(
    session: AsyncSession, workflow: Workflow, *, full: bool = False
) -> None:
    """черновик собирают как угодно - проверяется опубликованный и публикация"""
    if not (workflow.is_published or full):
        return
    # здесь, а не наверху: модели заявок сами импортируют модели воркфлоу
    from quoll.interactions.models import Branch, Interaction

    stages = (
        await session.scalars(select(Stage).where(Stage.workflow_id == workflow.id))
    ).all()
    edges = (
        await session.scalars(
            select(WorkflowTransition).where(
                WorkflowTransition.workflow_id == workflow.id,
                WorkflowTransition.is_active.is_(True),
            )
        )
    ).all()
    occupied = set(
        await session.scalars(
            select(Interaction.state_id)
            .where(
                Interaction.workflow_id == workflow.id,
                Interaction.state_id.is_not(None),
            )
            .distinct()
        )
    )
    occupied |= set(
        await session.scalars(
            select(Branch.state_id)
            .join(Interaction, Interaction.id == Branch.interaction_id)
            .where(
                Interaction.workflow_id == workflow.id,
                Branch.closed_at.is_(None),
            )
            .distinct()
        )
    )
    problems = graph_problems(
        [
            StageFacts(
                s.id,
                s.is_terminal,
                s.archived_at is not None,
                s.is_branch_stage,
                s.is_branch_start,
                s.is_parallel,
            )
            for s in stages
        ],
        edge_facts(edges),
        occupied,
        full=full,
    )
    if problems:
        raise DomainRuleException(
            409, "Workflow graph is broken: " + "; ".join(problems)
        )


def _journal(session, actor_id, event, target_type, target_id, old=None, new=None):
    record(
        session,
        actor_id=actor_id,
        event_type=event,
        target_type=target_type,
        target_id=target_id,
        old_value=old,
        new_value=new,
    )


async def publish(session: AsyncSession, workflow_id: int, actor_id: str) -> None:
    """публикуется проходимый граф целиком: заявки не должны застрять"""
    workflow = await lock_workflow(session, workflow_id)
    if workflow.is_published:
        raise DomainRuleException(409, "Workflow is already published")
    await check_graph(session, workflow, full=True)
    workflow.is_published = True
    _journal(
        session,
        actor_id,
        AuditEventType.WORKFLOW_PUBLISHED,
        TargetType.WORKFLOW,
        workflow.id,
    )
    await session.flush()


async def delete_workflow(
    session: AsyncSession, workflow_id: int, actor_id: str
) -> None:
    """только черновик: по опубликованному могли ехать заявки"""
    workflow = await lock_workflow(session, workflow_id)
    if workflow.is_published:
        raise DomainRuleException(409, "Published workflow cannot be deleted")
    _journal(
        session,
        actor_id,
        AuditEventType.WORKFLOW_DELETED,
        TargetType.WORKFLOW,
        workflow.id,
        old={"name": workflow.name},
    )
    await session.delete(workflow)
    await session.flush()


# --- стадии


async def create_stage(
    session: AsyncSession, schema: StageCreate, actor_id: str
) -> Stage:
    # новая стадия пуста и недостижима - граф она не ломает, проверять нечего
    await lock_workflow(session, schema.workflow_id)
    if schema.parent_stage_id is not None:
        parent = await session.get(Stage, schema.parent_stage_id)
        # один уровень: подшаг подшага не бывает
        if (
            parent is None
            or parent.workflow_id != schema.workflow_id
            or parent.parent_stage_id is not None
            or parent.is_branch_stage != schema.is_branch_stage
        ):
            raise DomainRuleException(
                400, "Parent is a main stage of the same workflow and level"
            )
    data = schema.model_dump()
    if schema.is_parallel and await session.scalar(
        select(Stage.id).where(
            Stage.workflow_id == schema.workflow_id,
            Stage.is_parallel.is_(True),
            Stage.archived_at.is_(None),
        )
    ):
        raise DomainRuleException(409, "Workflow already has a parallel stage")
    if schema.passive_after_days is not None and not schema.is_branch_stage:
        raise DomainRuleException(400, "Only branch stages are long-term")
    await _check_binds(
        session, schema.workflow_id, None, schema.is_branch_stage, data["fields"]
    )
    stage = Stage(**data)
    session.add(stage)
    await session.flush()
    _journal(
        session,
        actor_id,
        AuditEventType.STAGE_CREATED,
        TargetType.STAGE,
        stage.id,
        new=schema.model_dump(mode="json"),
    )
    await session.refresh(stage)
    return stage


async def _check_binds(
    session: AsyncSession,
    workflow_id: int,
    stage_id: int | None,
    is_branch: bool,
    fields,
) -> None:
    """П11: поля ветки - на шагах веток, договора - на шагах заявки; одна
    колонка на весь воркфлоу - одно поле"""
    binds = {f["bind"] for f in fields if f.get("bind")}
    wrong = {b for b in binds if b.startswith("branch.") != is_branch}
    if wrong:
        raise DomainRuleException(400, f"Bind {sorted(wrong)} is for another level")
    others = await session.scalars(
        select(Stage.fields).where(
            Stage.workflow_id == workflow_id,
            Stage.archived_at.is_(None),
            Stage.id.is_distinct_from(stage_id),
        )
    )
    taken = {f.get("bind") for other in others for f in other} & binds
    if taken:
        raise DomainRuleException(409, f"Bind {sorted(taken)} is on another stage")


async def _stage(session: AsyncSession, stage_id: int) -> Stage:
    stage = await session.get(Stage, stage_id)
    if stage is None:
        raise IdNotExistsException(Stage.__name__)
    return stage


async def update_stage(
    session: AsyncSession, stage_id: int, changes: StageUpdate, actor_id: str
) -> Stage:
    """имя, описание, позиция. Флаги стадии не меняются никогда (Р15)"""
    stage = await _stage(session, stage_id)
    await lock_workflow(session, stage.workflow_id)
    new = changes.model_dump(exclude_unset=True)
    if new.get("passive_after_days") is not None and not stage.is_branch_stage:
        raise DomainRuleException(400, "Only branch stages are long-term")
    if "fields" in new:
        await _check_binds(
            session, stage.workflow_id, stage.id, stage.is_branch_stage, new["fields"]
        )
    old = {field: getattr(stage, field) for field in new}
    for field, value in new.items():
        setattr(stage, field, value)
    _journal(
        session,
        actor_id,
        AuditEventType.STAGE_UPDATED,
        TargetType.STAGE,
        stage.id,
        old,
        new,
    )
    await session.flush()
    await session.refresh(stage)
    return stage


async def delete_stage(session: AsyncSession, stage_id: int, actor_id: str) -> None:
    """удаляется стадия черновика; у опубликованного - архивация"""
    stage = await _stage(session, stage_id)
    workflow = await lock_workflow(session, stage.workflow_id)
    if workflow.is_published:
        raise DomainRuleException(
            409, "Stage of a published workflow is archived, not deleted"
        )
    _journal(
        session,
        actor_id,
        AuditEventType.STAGE_DELETED,
        TargetType.STAGE,
        stage.id,
        old={"name": stage.name, "workflow_id": stage.workflow_id},
    )
    await session.delete(stage)
    await session.flush()


# --- рёбра


async def _check_ends(
    session: AsyncSession, workflow_id: int, stage_ids: list[int | None]
) -> None:
    for stage_id in stage_ids:
        if stage_id is None:
            continue
        stage = await _stage(session, stage_id)
        if stage.workflow_id != workflow_id:
            raise DomainRuleException(
                400, f"Stage '{stage_id}' belongs to another workflow"
            )
        if stage.archived_at is not None:
            raise DomainRuleException(409, f"Stage '{stage_id}' is archived")


async def _check_reject_to(session: AsyncSession, edge: WorkflowTransition) -> None:
    # отказ уводит на доработку, а не закрывает - иначе отклонить было бы нельзя
    if edge.reject_to_stage_id is not None:
        stage = await _stage(session, edge.reject_to_stage_id)
        source = (
            await _stage(session, edge.from_stage_id) if edge.from_stage_id else None
        )
        if stage.is_parallel:
            raise DomainRuleException(400, "Rejection cannot lead to a parallel stage")
        if stage.is_terminal or (
            source is not None and source.is_branch_stage != stage.is_branch_stage
        ):
            raise DomainRuleException(
                400, "reject_to_stage_id must be a working stage of the same part"
            )


def _check_rules(edge: WorkflowTransition) -> None:
    # то же держат CHECK-и, но здесь - с понятной причиной вместо 409
    if edge.requires_approval and edge.from_stage_id is None:
        raise DomainRuleException(
            400, "Entry transition cannot require approval: it is the acceptance"
        )
    if edge.reject_to_stage_id is not None and not edge.requires_approval:
        raise DomainRuleException(400, "reject_to_stage_id needs requires_approval")


async def _check_kinds(session: AsyncSession, edge: WorkflowTransition) -> None:
    from quoll.catalog.models import DocumentKind

    wanted = set(edge.required_document_kinds or [])
    known = set(
        await session.scalars(
            select(DocumentKind.code).where(DocumentKind.code.in_(wanted))
        )
    )
    if wanted - known:
        raise DomainRuleException(
            400, f"Unknown document kinds: {', '.join(sorted(wanted - known))}"
        )


async def create_transition(
    session: AsyncSession, schema: WorkflowTransitionCreate, actor_id: str
) -> WorkflowTransition:
    workflow = await lock_workflow(session, schema.workflow_id)
    await _check_ends(
        session,
        workflow.id,
        [schema.from_stage_id, schema.to_stage_id, schema.reject_to_stage_id],
    )
    edge = WorkflowTransition(**schema.model_dump())
    _check_rules(edge)
    await _check_reject_to(session, edge)
    await _check_kinds(session, edge)
    session.add(edge)
    await session.flush()
    await check_graph(session, workflow)
    _journal(
        session,
        actor_id,
        AuditEventType.TRANSITION_CREATED,
        TargetType.TRANSITION,
        edge.id,
        new={"from_stage_id": edge.from_stage_id, "to_stage_id": edge.to_stage_id},
    )
    await session.refresh(edge)
    return edge


async def _lock_edge(
    session: AsyncSession, transition_id: int
) -> tuple[Workflow, WorkflowTransition]:
    # воркфлоу раньше ребра - порядок из core/locking.py
    edge = await session.get(WorkflowTransition, transition_id)
    if edge is None:
        raise IdNotExistsException(WorkflowTransition.__name__)
    workflow = await lock_workflow(session, edge.workflow_id)
    return workflow, await lock_row(session, WorkflowTransition, transition_id)


async def update_transition(
    session: AsyncSession,
    transition_id: int,
    changes: WorkflowTransitionUpdate,
    actor_id: str,
) -> WorkflowTransition:
    """у опубликованного концы не перевешиваются - история ссылается на
    ребро, и оно стало бы значить другое. Деактивировать и создать новое"""
    workflow, edge = await _lock_edge(session, transition_id)
    new = changes.model_dump(exclude_unset=True)
    moved = _EDGE_ENDS & new.keys()
    if moved and workflow.is_published:
        raise PublishedGraphChangeException(sorted(moved))
    await _check_ends(
        session, workflow.id, [new.get(f) for f in moved | {"reject_to_stage_id"}]
    )

    old = {field: getattr(edge, field) for field in new}
    for field, value in new.items():
        setattr(edge, field, value)
    _check_rules(edge)
    await _check_reject_to(session, edge)
    await _check_kinds(session, edge)
    await session.flush()
    # имя и описание граф не ломают, концы у опубликованного не меняются
    if "is_active" in new:
        await check_graph(session, workflow)

    deactivated = old.get("is_active") is True and new.get("is_active") is False
    event = (
        AuditEventType.TRANSITION_DEACTIVATED
        if deactivated
        else AuditEventType.TRANSITION_UPDATED
    )
    _journal(session, actor_id, event, TargetType.TRANSITION, edge.id, old, new)
    await session.refresh(edge)
    return edge


async def delete_transition(
    session: AsyncSession, transition_id: int, actor_id: str
) -> None:
    """удаляется ребро черновика; у опубликованного на ребро ссылается история"""
    workflow, edge = await _lock_edge(session, transition_id)
    if workflow.is_published:
        raise DomainRuleException(
            409, "Transition of a published workflow is deactivated, not deleted"
        )
    _journal(
        session,
        actor_id,
        AuditEventType.TRANSITION_DELETED,
        TargetType.TRANSITION,
        edge.id,
        old={"from_stage_id": edge.from_stage_id, "to_stage_id": edge.to_stage_id},
    )
    await session.delete(edge)
    await session.flush()


async def change_start_stage(
    session: AsyncSession, workflow_id: int, stage_id: int, actor_id: str
) -> WorkflowTransition:
    """одной операцией: после любой из двух правок по отдельности начальных
    стадий было бы ноль или две"""
    workflow = await lock_workflow(session, workflow_id)
    await _check_ends(session, workflow.id, [stage_id])
    if (await _stage(session, stage_id)).is_terminal:
        raise DomainRuleException(400, "Start stage cannot be terminal")
    current = (
        await session.scalars(
            select(WorkflowTransition).where(
                WorkflowTransition.workflow_id == workflow.id,
                WorkflowTransition.from_stage_id.is_(None),
                WorkflowTransition.is_active.is_(True),
            )
        )
    ).all()
    if [e.to_stage_id for e in current] == [stage_id]:
        return current[0]

    for edge in current:
        edge.is_active = False
    # сначала снять старое - иначе частичный уникальный индекс увидит два входа
    await session.flush()
    entry = WorkflowTransition(
        workflow_id=workflow.id,
        from_stage_id=None,
        to_stage_id=stage_id,
        name=current[0].name if current else "Начало",
    )
    session.add(entry)
    await session.flush()
    await check_graph(session, workflow)
    _journal(
        session,
        actor_id,
        AuditEventType.START_STAGE_CHANGED,
        TargetType.WORKFLOW,
        workflow.id,
        old={"stage_id": current[0].to_stage_id if current else None},
        new={"stage_id": stage_id},
    )
    await session.refresh(entry)
    return entry


# --- архивация


def _fits(target: Stage, archived: Stage) -> bool:
    """перенос не меняет смысла и не увеличивает ничью загрузку"""
    return (
        target.id != archived.id
        and target.workflow_id == archived.workflow_id
        and target.archived_at is None
        and target.is_terminal == archived.is_terminal
        and target.is_branch_stage == archived.is_branch_stage
        and target.is_parallel == archived.is_parallel
    )


async def _open_agreements(session: AsyncSession, workflow_id: int) -> bool:
    from quoll.interactions.models import (
        AgreementStatus,
        Interaction,
        SupplementaryAgreement,
    )

    return bool(
        await session.scalar(
            select(SupplementaryAgreement.id)
            .join(Interaction, Interaction.id == SupplementaryAgreement.interaction_id)
            .where(
                Interaction.workflow_id == workflow_id,
                SupplementaryAgreement.status.in_(
                    [AgreementStatus.DRAFT, AgreementStatus.PENDING]
                ),
            )
            .limit(1)
        )
    )


async def _previous_stage(session: AsyncSession, stage: Stage) -> Stage | None:
    """предыдущая действующая по позиции, при равных - меньший id"""
    stmt = (
        select(Stage)
        .where(
            Stage.workflow_id == stage.workflow_id,
            Stage.archived_at.is_(None),
            Stage.id != stage.id,
            (Stage.position < stage.position)
            | ((Stage.position == stage.position) & (Stage.id < stage.id)),
        )
        .order_by(Stage.position.desc(), Stage.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def archive_stage(
    session: AsyncSession,
    stage_id: int,
    relocate_to_stage_id: int | None,
    actor_id: str,
) -> Stage:
    """стадию живого графа не удаляют: на неё ссылаются история и документы.
    Заявки переезжают, рёбра деактивируются, граф проверяется по итогу.

    порядок - core/locking.py: воркфлоу, архивируемая стадия, заявки на ней
    по id, рёбра. Цель переноса не блокируется: параллельную архивацию цели
    исключает строка воркфлоу, а NO KEY UPDATE на ней дал бы цикл с
    переходом из архивируемой стадии в цель
    """
    from quoll.interactions.models import (
        Branch,
        Interaction,
        InteractionStageHistory,
        StageChangeKind,
    )

    stage = await _stage(session, stage_id)
    workflow = await lock_workflow(session, stage.workflow_id)
    if not workflow.is_published:
        raise DomainRuleException(
            409, "Stage of a draft workflow is deleted, not archived"
        )
    # UPDATE берёт строку стадии: переход в неё, начатый раньше, архивация
    # дождётся, начатый позже - увидит архив и откажет
    archived = (
        await session.execute(
            update(Stage)
            .where(Stage.id == stage.id, Stage.archived_at.is_(None))
            .values(archived_at=func.now())
            .returning(Stage.id)
        )
    ).scalar_one_or_none()
    if archived is None:
        raise DomainRuleException(409, "Stage is already archived")
    await session.refresh(stage)
    if stage.is_parallel and await _open_agreements(session, workflow.id):
        raise DomainRuleException(
            409, "Finish or cancel open supplementary agreements first"
        )

    # ветки своих блокировок не имеют - берём их взаимодействия
    # закрытые досрочно ветки тоже переезжают - иначе RESUME не вернул бы
    # их на архивный шаг (П6). С терминального шага переносим только открытые:
    # итог завершённых не подменяем
    movable = [Branch.state_id == stage.id]
    if stage.is_terminal:
        movable.append(Branch.closed_at.is_(None))
    on_branch = select(Branch.interaction_id).where(*movable).scalar_subquery()
    locked = list(
        await session.scalars(
            select(Interaction.id)
            .where((Interaction.state_id == stage.id) | Interaction.id.in_(on_branch))
            .order_by(Interaction.id)
            .with_for_update(of=Interaction.__table__, key_share=True)
        )
    )
    standing = list(
        await session.scalars(
            select(Interaction.id).where(Interaction.state_id == stage.id)
        )
    )
    branches = list(
        await session.execute(select(Branch.id, Branch.interaction_id).where(*movable))
    )
    target = None
    if locked:
        target = (
            await _stage(session, relocate_to_stage_id)
            if relocate_to_stage_id is not None
            else await _previous_stage(session, stage)
        )
        if target is None or not _fits(target, stage):
            raise DomainRuleException(
                409,
                "Relocation target must be an active stage of this workflow with "
                "the same terminality and level; pass it explicitly",
            )
        await session.execute(
            update(Interaction)
            .where(Interaction.id.in_(standing))
            .values(state_id=target.id, stall_since=func.now())
        )
        session.add_all(
            InteractionStageHistory(
                interaction_id=interaction_id,
                from_stage_id=stage.id,
                to_stage_id=target.id,
                kind=StageChangeKind.RELOCATION,
                actor_id=actor_id,
                comment=f"stage '{stage.name}' archived",
            )
            for interaction_id in standing
        )
        if branches:
            await session.execute(
                update(Branch)
                .where(Branch.id.in_([b.id for b in branches]))
                .values(state_id=target.id, stall_since=func.now())
            )
            session.add_all(
                InteractionStageHistory(
                    interaction_id=b.interaction_id,
                    branch_id=b.id,
                    from_stage_id=stage.id,
                    to_stage_id=target.id,
                    kind=StageChangeKind.RELOCATION,
                    actor_id=actor_id,
                    comment=f"stage '{stage.name}' archived",
                )
                for b in branches
            )

    edges = await session.scalars(
        select(WorkflowTransition)
        .where(
            WorkflowTransition.is_active.is_(True),
            (WorkflowTransition.from_stage_id == stage.id)
            | (WorkflowTransition.to_stage_id == stage.id),
        )
        .order_by(WorkflowTransition.id)
        .with_for_update(of=WorkflowTransition.__table__, key_share=True)
    )
    for edge in edges:
        edge.is_active = False
    await session.flush()
    await check_graph(session, workflow)

    _journal(
        session,
        actor_id,
        AuditEventType.STAGE_ARCHIVED,
        TargetType.STAGE,
        stage.id,
        new={
            "relocated": len(standing),
            "relocated_branches": len(branches),
            "to_stage_id": target.id if target else None,
        },
    )
    return stage


# --- воркфлоу и шаблоны на рёбрах


async def create_workflow(
    session: AsyncSession, schema: WorkflowCreate, actor_id: str
) -> Workflow:
    workflow = Workflow(**schema.model_dump())
    session.add(workflow)
    await session.flush()
    _journal(
        session,
        actor_id,
        AuditEventType.WORKFLOW_CREATED,
        TargetType.WORKFLOW,
        workflow.id,
        new=schema.model_dump(mode="json"),
    )
    await session.refresh(workflow)
    return workflow


async def update_workflow(
    session: AsyncSession, workflow_id: int, changes: WorkflowUpdate, actor_id: str
) -> Workflow:
    workflow = await lock_workflow(session, workflow_id)
    new = changes.model_dump(exclude_unset=True)
    old = {field: getattr(workflow, field) for field in new}
    for field, value in new.items():
        setattr(workflow, field, value)
    _journal(
        session,
        actor_id,
        AuditEventType.WORKFLOW_UPDATED,
        TargetType.WORKFLOW,
        workflow.id,
        old,
        new,
    )
    await session.flush()
    await session.refresh(workflow)
    return workflow


def journal_template(
    session: AsyncSession,
    actor_id: str,
    transition_id: int,
    attachment_id: int,
    *,
    linked: bool,
) -> None:
    _journal(
        session,
        actor_id,
        AuditEventType.TEMPLATE_ATTACHMENT_LINKED
        if linked
        else AuditEventType.TEMPLATE_ATTACHMENT_UNLINKED,
        TargetType.TRANSITION,
        transition_id,
        new={"attachment_id": attachment_id},
    )
