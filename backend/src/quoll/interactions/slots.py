"""Активные и пассивные слоты (Д19).

в предел КАМа входят только активные. В пассивные заявку уводит сторож,
когда она давно без действий на долгосрочных этапах; КАМ может увести и
сам. Обратно - только сам КАМ кнопкой, с проверкой свободного места.
Пассивная ничем не ограничена, и никакое действие её не активирует
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.core.exceptions import DomainRuleException, OperationForbiddenException
from quoll.interactions.capacity_policy import (
    assert_can_keep_working,
    counts_toward_capacity,
)
from quoll.interactions.models import (
    AgreementStatus,
    Branch,
    DocumentStatus,
    Interaction,
    InteractionAssignment,
    InteractionDocument,
    InteractionRequest,
    InteractionStageHistory,
    InteractionStageValues,
    PauseState,
    RequestStatus,
    SidePointer,
    SidePointerStatus,
    SlotKind,
    SupplementaryAgreement,
)
from quoll.interactions.scope import InteractionScope, lock_interaction_scope
from quoll.workflows.models import Stage


async def last_activity(session: AsyncSession, interaction: Interaction) -> datetime:
    """последнее действие по заявке и её веткам (§7.1)"""
    iid = interaction.id
    moments = [
        select(func.max(InteractionStageHistory.created_at)).where(
            InteractionStageHistory.interaction_id == iid
        ),
        select(func.max(InteractionStageValues.updated_at)).where(
            InteractionStageValues.interaction_id == iid
        ),
        select(func.max(InteractionDocument.created_at)).where(
            InteractionDocument.interaction_id == iid
        ),
        select(
            func.greatest(
                func.max(InteractionRequest.created_at),
                func.max(InteractionRequest.decided_at),
            )
        ).where(InteractionRequest.interaction_id == iid),
        select(func.max(InteractionAssignment.assigned_at)).where(
            InteractionAssignment.interaction_id == iid
        ),
    ]
    found = [await session.scalar(m) for m in moments]
    found += [interaction.slot_changed_at, interaction.no_return_at]
    return max(m for m in found if m is not None)


async def passive_problem(
    session: AsyncSession, interaction: Interaction, *, idle: bool
) -> str | None:
    """почему заявку нельзя увести в пассивные; None - можно. idle=False -
    без срока бездействия (ручной перевод)"""
    if interaction.no_return_at is None:
        return "contract is not signed yet"
    if interaction.closed_at is not None:
        return "interaction is closed"
    open_branches = list(
        await session.execute(
            select(Branch.pause_state, Stage.passive_after_days)
            .join(Stage, Stage.id == Branch.state_id)
            .where(Branch.interaction_id == interaction.id, Branch.closed_at.is_(None))
        )
    )
    if not open_branches:
        return "no open branches"
    working = [days for state, days in open_branches if state == PauseState.ACTIVE]
    if any(days is None for days in working):
        return "a branch is on a short-term step"
    if await _waiting(session, interaction.id):
        return "something waits for a decision"
    if idle:
        needed = max((d for d in working if d is not None), default=0)
        quiet = await session.scalar(
            select(func.now() - func.make_interval(0, 0, 0, needed))
        )
        if await last_activity(session, interaction) > quiet:
            return "there were recent actions"
    return None


async def _waiting(session: AsyncSession, interaction_id: int) -> bool:
    """ждущие просьбы, правки шагов, файлы и незавершённое ДС - это ещё работа"""
    checks = [
        select(SupplementaryAgreement.id).where(
            SupplementaryAgreement.interaction_id == interaction_id,
            SupplementaryAgreement.status.in_(
                [AgreementStatus.DRAFT, AgreementStatus.PENDING]
            ),
        ),
        select(InteractionRequest.id).where(
            InteractionRequest.interaction_id == interaction_id,
            InteractionRequest.status == RequestStatus.PENDING,
        ),
        select(InteractionStageValues.id).where(
            InteractionStageValues.interaction_id == interaction_id,
            InteractionStageValues.pending_values.is_not(None),
        ),
        select(InteractionDocument.id).where(
            InteractionDocument.interaction_id == interaction_id,
            InteractionDocument.status == DocumentStatus.PENDING,
        ),
        select(SidePointer.id).where(
            SidePointer.interaction_id == interaction_id,
            SidePointer.status == SidePointerStatus.ACTIVE,
        ),
    ]
    for check in checks:
        if await session.scalar(check.limit(1)) is not None:
            return True
    return False


async def _owner_scope(
    session: AsyncSession, interaction_id: int, actor_id: str
) -> InteractionScope:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    # только сам КАМ: руководитель слотами не управляет (Д19)
    if scope.interaction.owner_id != actor_id:
        raise OperationForbiddenException("change the slot of this interaction")
    return scope


async def activate(
    session: AsyncSession, *, interaction_id: int, actor_id: str
) -> Interaction:
    from quoll.interactions.project_service import set_slot

    scope = await _owner_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if interaction.slot == SlotKind.ACTIVE:
        raise DomainRuleException(409, "Interaction is already active")
    stage = await session.get(Stage, interaction.state_id)
    # место - по новому виду слота; на паузе заявка его и так не занимает
    delta = int(
        counts_toward_capacity(stage, interaction.is_paused, SlotKind.ACTIVE)
    ) - int(counts_toward_capacity(stage, interaction.is_paused, SlotKind.PASSIVE))
    await assert_can_keep_working(session, scope.owner, delta)
    await set_slot(session, interaction, SlotKind.ACTIVE, actor_id)
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def passivate(
    session: AsyncSession, *, interaction_id: int, actor_id: str
) -> Interaction:
    from quoll.interactions.project_service import set_slot

    scope = await _owner_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if interaction.slot == SlotKind.PASSIVE:
        raise DomainRuleException(409, "Interaction is already passive")
    if problem := await passive_problem(session, interaction, idle=False):
        raise DomainRuleException(409, f"Cannot be passive: {problem}")
    await set_slot(session, interaction, SlotKind.PASSIVE, actor_id)
    await session.flush()
    await session.refresh(interaction)
    return interaction
