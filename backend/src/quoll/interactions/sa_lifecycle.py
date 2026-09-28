"""Жизненный цикл допсоглашения, который трогают чужие модули: открытие при
входе прохождения на шаг 4.1, возврат в черновик, отмена (П7).

листовой модуль - только модели и журнал: его зовут step_hooks и
transition_service, а они не должны зависеть от sa_service
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.core.exceptions import DomainRuleException
from quoll.interactions.models import (
    AgreementStatus,
    Interaction,
    InteractionStageHistory,
    SidePointer,
    StageChangeKind,
    SupplementaryAgreement,
)

# sentinel: cancel_open без pass_id отменяет все незавершённые ДС заявки
ANY = object()


def label(sa: SupplementaryAgreement) -> str:
    return f"ДС №{sa.number}" if sa.number else f"ДС #{sa.id}"


def history(
    session: AsyncSession,
    interaction: Interaction,
    kind: StageChangeKind,
    actor_id: str | None,
    *,
    payload: dict[str, Any],
    branch_id: int | None = None,
    from_stage_id: int | None = None,
    to_stage_id: int | None = None,
    side_pointer_id: int | None = None,
    comment: str | None = None,
) -> None:
    """событие истории; у событий заявки без движения - её шаг с обеих сторон"""
    if branch_id is None and from_stage_id is None and to_stage_id is None:
        from_stage_id = to_stage_id = interaction.state_id
    session.add(
        InteractionStageHistory(
            interaction_id=interaction.id,
            branch_id=branch_id,
            side_pointer_id=side_pointer_id,
            from_stage_id=from_stage_id,
            to_stage_id=to_stage_id,
            kind=kind,
            actor_id=actor_id,
            comment=comment,
            payload=payload,
        )
    )


async def pass_history(
    session: AsyncSession,
    interaction: Interaction,
    kind: StageChangeKind,
    actor_id: str | None,
    sa: SupplementaryAgreement,
    *,
    payload: dict[str, Any],
    comment: str | None = None,
) -> None:
    """событие ДС: у доп. прохождения с обеих сторон - шаг указателя, а не
    заявки (§3.6)"""
    stage_id = interaction.state_id
    if sa.side_pointer_id is not None:
        stage_id = (await session.get(SidePointer, sa.side_pointer_id)).stage_id
    history(
        session,
        interaction,
        kind,
        actor_id,
        payload=payload,
        from_stage_id=stage_id,
        to_stage_id=stage_id,
        side_pointer_id=sa.side_pointer_id,
        comment=comment,
    )


def journal(
    session: AsyncSession,
    actor_id: str | None,
    event: AuditEventType,
    sa: SupplementaryAgreement,
    old: dict | None = None,
    new: dict | None = None,
) -> None:
    record(
        session,
        actor_id=actor_id,
        event_type=event,
        target_type=TargetType.SUPPLEMENTARY_AGREEMENT,
        target_id=sa.id,
        old_value=old,
        new_value=new,
    )


async def open_for_pass(
    session: AsyncSession,
    interaction: Interaction,
    pass_id: int | None,
    actor_id: str | None,
) -> SupplementaryAgreement:
    """прохождение встало на шаг обработчика - открывается новое ДС (I5).

    незавершённое ДС уже есть - последний рубеж I6 (доп. и основной на 4.1
    одновременно уже отсечены Д33/Д46, это только страховка)
    """
    unfinished = await session.scalar(
        select(SupplementaryAgreement).where(
            SupplementaryAgreement.interaction_id == interaction.id,
            SupplementaryAgreement.status.in_(
                [AgreementStatus.DRAFT, AgreementStatus.PENDING]
            ),
        )
    )
    if unfinished is not None:
        raise DomainRuleException(409, f"Agreement {unfinished.id} is not finished yet")
    sa = SupplementaryAgreement(
        interaction_id=interaction.id,
        side_pointer_id=pass_id,
        status=AgreementStatus.DRAFT,
        created_by=actor_id,
    )
    session.add(sa)
    await session.flush()
    await pass_history(
        session,
        interaction,
        StageChangeKind.SA_OPENED,
        actor_id,
        sa,
        payload={"sa_id": sa.id, "side_pointer_id": pass_id},
    )
    journal(session, actor_id, AuditEventType.SA_OPENED, sa)
    return sa


async def return_to_draft(
    session: AsyncSession,
    interaction: Interaction,
    sa: SupplementaryAgreement,
    reason: str,
    actor_id: str | None,
    kind: StageChangeKind = StageChangeKind.SA_RETURNED,
) -> None:
    """просьба об одобрении отменена, отозвана или отклонена - ДС снова
    черновик. kind - SA_REJECTED при отказе, иначе SA_RETURNED"""
    if sa.status != AgreementStatus.PENDING:
        return
    sa.status = AgreementStatus.DRAFT
    await pass_history(
        session,
        interaction,
        kind,
        actor_id,
        sa,
        payload={"sa_id": sa.id, "reason": reason},
        comment=reason,
    )
    event = (
        AuditEventType.SA_REJECTED
        if kind == StageChangeKind.SA_REJECTED
        else AuditEventType.SA_RETURNED
    )
    journal(session, actor_id, event, sa, new={"reason": reason})


async def cancel_open(
    session: AsyncSession,
    interaction: Interaction,
    actor_id: str | None,
    reason: str,
    pass_id: int | None = ANY,
) -> None:
    """заявка закрывается или прохождение уходит с шага - незавершённое ДС
    отменяется. pass_id=ANY (умолчание) - все прохождения; иначе только своё"""
    conditions = [
        SupplementaryAgreement.interaction_id == interaction.id,
        SupplementaryAgreement.status.in_(
            [AgreementStatus.DRAFT, AgreementStatus.PENDING]
        ),
    ]
    if pass_id is not ANY:
        conditions.append(
            SupplementaryAgreement.side_pointer_id.is_not_distinct_from(pass_id)
        )
    open_agreements = await session.scalars(
        select(SupplementaryAgreement)
        .where(*conditions)
        .execution_options(populate_existing=True)
    )
    for sa in open_agreements:
        sa.status = AgreementStatus.CANCELLED
        sa.decided_by = actor_id
        sa.decided_at = func.now()
        sa.decision_comment = reason
        await pass_history(
            session,
            interaction,
            StageChangeKind.SA_CANCELLED,
            actor_id,
            sa,
            payload={"sa_id": sa.id, "reason": reason},
            comment=reason,
        )
        journal(
            session, actor_id, AuditEventType.SA_CANCELLED, sa, new={"reason": reason}
        )
