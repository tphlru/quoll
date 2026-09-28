"""Доп. указатель: проходит доп. шаги, пока основной уже ушёл дальше (Д30-Д50).

ходит по тем же рёбрам и проверкам, что основной указатель (check_step,
active_edge, lock_target_stage), но не меняет interactions.state_id,
branches.state_id, no_return_at и слоты (I4). Правила договора, open_branches
и place() сюда не заходят
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.core.exceptions import (
    DomainRuleException,
    IdNotExistsException,
    OperationForbiddenException,
    StaleStateException,
    WorkflowNotPublishedException,
)
from quoll.interactions import step_hooks
from quoll.interactions.access_policy import can_change
from quoll.interactions.models import (
    Branch,
    DocumentStatus,
    Interaction,
    InteractionDocument,
    InteractionRequest,
    InteractionStageHistory,
    InteractionStageValues,
    RequestStatus,
    SidePointer,
    SidePointerStatus,
    StageChangeKind,
)
from quoll.interactions.notify import notify
from quoll.interactions.scope import InteractionScope, lock_interaction_scope
from quoll.interactions.transition_service import (
    active_edge,
    check_step,
    lock_target_stage,
    share_stage,
)
from quoll.notifications import kinds
from quoll.workflows.graph_policy import EdgeFacts, leads_to
from quoll.workflows.models import Stage, Workflow, WorkflowTransition


def segment(
    edges: list[EdgeFacts], stages_by_id: dict[int, Stage], entry_id: int
) -> tuple[set[int], list[EdgeFacts]]:
    """доп. стадии сегмента входа entry_id (достижимые по рёбрам между доп.
    стадиями) и его выходы - прямые рёбра из сегмента в стадию не-доп."""
    side = {i for i, s in stages_by_id.items() if s.is_side}
    inner = [e for e in edges if e.from_stage_id in side and e.to_stage_id in side]
    members = {i for i in side if i == entry_id or leads_to(inner, entry_id, i)}
    exits = [
        e
        for e in edges
        if e.from_stage_id in members and e.to_stage_id not in side and not e.backward
    ]
    return members, exits


def return_points(exits: list[EdgeFacts], stages_by_id: dict[int, Stage]) -> set[int]:
    """нетерминальные цели выходов (Д33)"""
    return {e.to_stage_id for e in exits if not stages_by_id[e.to_stage_id].is_terminal}


async def positions(session: AsyncSession, interaction: Interaction) -> set[int]:
    """позиции основного указателя: свой шаг плюс шаги всех веток заявки"""
    result = set()
    if interaction.state_id is not None:
        result.add(interaction.state_id)
    branch_states = await session.scalars(
        select(Branch.state_id).where(
            Branch.interaction_id == interaction.id, Branch.state_id.is_not(None)
        )
    )
    result |= set(branch_states)
    return result


def passed(edges: list[EdgeFacts], point: int, current_positions: set[int]) -> bool:
    """основной указатель прошёл точку: стоит на ней или дальше по прямым рёбрам"""
    forward = [e for e in edges if not e.backward]
    return any(p == point or leads_to(forward, point, p) for p in current_positions)


async def _graph(
    session: AsyncSession, workflow_id: int
) -> tuple[list[EdgeFacts], dict[int, Stage]]:
    stages = list(
        await session.scalars(select(Stage).where(Stage.workflow_id == workflow_id))
    )
    edges = list(
        await session.scalars(
            select(WorkflowTransition).where(
                WorkflowTransition.workflow_id == workflow_id,
                WorkflowTransition.is_active.is_(True),
            )
        )
    )
    facts = [
        EdgeFacts(e.from_stage_id, e.to_stage_id, e.is_irreversible, e.is_backward)
        for e in edges
    ]
    return facts, {s.id: s for s in stages}


async def _active(session: AsyncSession, interaction_id: int) -> SidePointer | None:
    return await session.scalar(
        select(SidePointer).where(
            SidePointer.interaction_id == interaction_id,
            SidePointer.status == SidePointerStatus.ACTIVE,
        )
    )


async def load_active(
    session: AsyncSession, interaction_id: int, pointer_id: int
) -> SidePointer:
    pointer = await session.get(SidePointer, pointer_id, populate_existing=True)
    if pointer is None or pointer.interaction_id != interaction_id:
        raise IdNotExistsException(SidePointer.__name__)
    if pointer.status != SidePointerStatus.ACTIVE:
        raise DomainRuleException(409, f"Side pointer is {pointer.status}")
    return pointer


async def _return_points(
    session: AsyncSession, interaction: Interaction, entry: Stage
) -> tuple[set[int], bool]:
    """точки возврата сегмента входа entry и прошёл ли основной их все (Д33)"""
    edges, stages_by_id = await _graph(session, interaction.workflow_id)
    _, exits = segment(edges, stages_by_id, entry.id)
    points = return_points(exits, stages_by_id)
    current = await positions(session, interaction)
    return points, all(passed(edges, p, current) for p in points)


async def passed_segment(
    session: AsyncSession, interaction: Interaction, stage: Stage
) -> bool:
    """Д46: все точки возврата сегмента входа stage уже пройдены основным"""
    points, all_passed = await _return_points(session, interaction, stage)
    return bool(points) and all_passed


async def start(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    stage_id: int,
    comment: str | None,
) -> SidePointer:
    await share_stage(session, stage_id)
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    if not can_change(scope.actor, scope.ownership):
        raise OperationForbiddenException("start a side pointer of this interaction")
    interaction = scope.interaction
    if interaction.state_id is None or interaction.closed_at is not None:
        raise DomainRuleException(409, "Only an interaction in work has side pointers")
    workflow = await session.get(Workflow, interaction.workflow_id)
    if workflow is None or not workflow.is_published:
        raise WorkflowNotPublishedException(interaction.workflow_id)
    entry = (
        await session.execute(
            select(Stage)
            .where(Stage.id == stage_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if entry is None or entry.workflow_id != interaction.workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    if not entry.is_side:
        raise DomainRuleException(400, "Stage is not a side stage")
    if entry.archived_at is not None:
        raise DomainRuleException(409, f"Stage '{stage_id}' is archived")
    if entry.is_branch_stage:
        raise DomainRuleException(409, "Side steps of branches are not supported yet")

    # вход - по любому ребру из основной стадии, прошедшему её проверки на
    # значениях и файлах основного прохождения (Д49); ответ - по первому
    entry_edges = list(
        await session.scalars(
            select(WorkflowTransition)
            .join(Stage, Stage.id == WorkflowTransition.from_stage_id)
            .where(
                WorkflowTransition.to_stage_id == entry.id,
                WorkflowTransition.is_active.is_(True),
                Stage.is_side.is_(False),
            )
            .order_by(WorkflowTransition.id)
        )
    )
    if not entry_edges:
        raise DomainRuleException(400, "Stage is not an entry of side steps")
    problems = []
    for edge in entry_edges:
        if edge.requires_approval:
            problems.append(
                "Side pointer cannot enter through a transition needing approval"
            )
            continue
        source = await session.get(Stage, edge.from_stage_id)
        try:
            await check_step(session, interaction, source, edge, approved=False)
        except DomainRuleException as err:
            problems.append(err.message)
            continue
        break
    else:
        raise DomainRuleException(409, problems[0])

    active = await _active(session, interaction.id)
    if active is not None:
        raise DomainRuleException(409, f"Side pointer {active.id} is already active")
    points, all_passed = await _return_points(session, interaction, entry)
    if not points:
        raise DomainRuleException(409, "Side steps have no way back to the main route")
    if not all_passed:
        raise DomainRuleException(
            409,
            "Main route has not passed these steps yet, move the interaction itself",
        )

    pointer = SidePointer(
        interaction_id=interaction.id,
        entry_stage_id=entry.id,
        stage_id=entry.id,
        status=SidePointerStatus.ACTIVE,
        started_by=actor_id,
    )
    session.add(pointer)
    await session.flush()
    session.add(
        InteractionStageHistory(
            interaction_id=interaction.id,
            side_pointer_id=pointer.id,
            from_stage_id=None,
            to_stage_id=entry.id,
            kind=StageChangeKind.SIDE_STARTED,
            actor_id=actor_id,
            comment=comment,
        )
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.SIDE_POINTER_STARTED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        new_value={"side_pointer_id": pointer.id, "stage_id": entry.id},
    )
    await step_hooks.enter(session, scope, entry, pointer.id, actor_id=actor_id)
    await session.flush()
    await session.refresh(pointer)
    return pointer


async def move(
    session: AsyncSession,
    *,
    interaction_id: int,
    pointer_id: int,
    actor_id: str,
    to_stage_id: int,
    expected_state_id: int | None,
    comment: str | None,
) -> SidePointer:
    pre = await step_hooks.pre_share(session, interaction_id, pointer_id, to_stage_id)
    for stage_id in sorted({to_stage_id} | pre):
        await share_stage(session, stage_id)
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    if not can_change(scope.actor, scope.ownership):
        raise OperationForbiddenException("move a side pointer of this interaction")
    pointer = await load_active(session, interaction_id, pointer_id)
    if pointer.stage_id != expected_state_id:
        raise StaleStateException("Side pointer stage", pointer.stage_id)
    return await move_locked(
        session,
        scope,
        pointer,
        to_stage_id,
        comment,
        approved=False,
        shared={to_stage_id} | pre,
    )


async def move_locked(
    session: AsyncSession,
    scope: InteractionScope,
    pointer: SidePointer,
    to_stage_id: int,
    comment: str | None,
    *,
    approved: bool,
    shared: set[int],
) -> SidePointer:
    """ход под уже захваченной областью - его зовёт и одобрение просьбы"""
    interaction = scope.interaction
    actor_id = scope.actor.id
    if to_stage_id == pointer.stage_id:
        raise DomainRuleException(409, "Side pointer is already on this stage")
    current = await session.get(Stage, pointer.stage_id)
    target = await lock_target_stage(session, to_stage_id)
    if target.workflow_id != interaction.workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    edge = await active_edge(session, interaction.workflow_id, current, target)
    if edge is None:
        raise DomainRuleException(409, "No active transition between these stages")
    if edge.is_backward and not comment:
        raise DomainRuleException(422, "Backward transition needs a comment")
    await check_step(
        session,
        interaction,
        current,
        edge,
        approved=approved,
        side_pointer_id=pointer.id,
    )

    if not target.is_side:
        if edge.is_backward:
            raise DomainRuleException(
                409, "Side pointer leaves only forward; cancel it instead"
            )
        await step_hooks.leave(
            session, scope, current, edge, pointer.id, shared, actor_id, comment
        )
        await _finish(session, pointer, actor_id, comment, edge, target)
    else:
        if edge.is_backward:
            await step_hooks.abandon(
                session, scope, current, pointer.id, comment or "", actor_id
            )
        else:
            await step_hooks.leave(
                session, scope, current, edge, pointer.id, shared, actor_id, comment
            )
        pointer.stage_id = target.id
        session.add(
            InteractionStageHistory(
                interaction_id=interaction.id,
                side_pointer_id=pointer.id,
                from_stage_id=current.id,
                to_stage_id=target.id,
                transition_id=edge.id,
                kind=StageChangeKind.TRANSITION,
                actor_id=actor_id,
                comment=comment,
            )
        )
        record(
            session,
            actor_id=actor_id,
            event_type=AuditEventType.STAGE_TRANSITIONED,
            target_type=TargetType.INTERACTION,
            target_id=interaction.id,
            old_value={"state_id": current.id, "side_pointer_id": pointer.id},
            new_value={"state_id": target.id, "side_pointer_id": pointer.id},
        )
        await step_hooks.enter(session, scope, target, pointer.id, actor_id=actor_id)

    if edge.is_backward:
        await notify(
            session,
            kinds.BACKWARD_MOVE,
            scope,
            context={
                "branch": "",
                "from_stage": current.name,
                "to_stage": target.name,
                "comment": comment,
            },
        )
    await session.flush()
    await session.refresh(pointer)
    return pointer


async def _finish(
    session: AsyncSession,
    pointer: SidePointer,
    actor_id: str | None,
    comment: str | None,
    edge: WorkflowTransition,
    target: Stage,
) -> None:
    pointer.status = SidePointerStatus.FINISHED
    pointer.finished_by = actor_id
    pointer.finished_at = func.now()
    pointer.finish_comment = comment
    session.add(
        InteractionStageHistory(
            interaction_id=pointer.interaction_id,
            side_pointer_id=pointer.id,
            from_stage_id=pointer.stage_id,
            to_stage_id=pointer.stage_id,
            transition_id=None,
            kind=StageChangeKind.SIDE_FINISHED,
            actor_id=actor_id,
            comment=comment,
            payload={"transition_id": edge.id, "exit_to": target.id},
        )
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.SIDE_POINTER_FINISHED,
        target_type=TargetType.INTERACTION,
        target_id=pointer.interaction_id,
        new_value={"side_pointer_id": pointer.id, "stage_id": pointer.stage_id},
    )
    await _quiet(session, pointer, comment or "side pass finished")


async def cancel(
    session: AsyncSession,
    *,
    interaction_id: int,
    pointer_id: int,
    actor_id: str,
    comment: str | None,
) -> SidePointer:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    if not can_change(scope.actor, scope.ownership):
        raise OperationForbiddenException("cancel a side pointer of this interaction")
    pointer = await load_active(session, interaction_id, pointer_id)
    return await cancel_locked(session, scope, pointer, comment or "cancelled")


async def cancel_locked(
    session: AsyncSession,
    scope: InteractionScope,
    pointer: SidePointer,
    reason: str,
) -> SidePointer:
    """зовут отмена, закрытие заявки и переход в терминальную"""
    actor_id = scope.actor.id if scope.actor else None
    stage = await session.get(Stage, pointer.stage_id)
    await step_hooks.abandon(session, scope, stage, pointer.id, reason, actor_id)
    await _quiet(session, pointer, reason)
    pointer.status = SidePointerStatus.CANCELLED
    pointer.finished_by = actor_id
    pointer.finished_at = func.now()
    pointer.finish_comment = reason
    session.add(
        InteractionStageHistory(
            interaction_id=pointer.interaction_id,
            side_pointer_id=pointer.id,
            from_stage_id=pointer.stage_id,
            to_stage_id=pointer.stage_id,
            kind=StageChangeKind.SIDE_CANCELLED,
            actor_id=actor_id,
            comment=reason,
        )
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.SIDE_POINTER_CANCELLED,
        target_type=TargetType.INTERACTION,
        target_id=pointer.interaction_id,
        new_value={"side_pointer_id": pointer.id, "reason": reason},
    )
    await session.flush()
    await session.refresh(pointer)
    return pointer


async def _quiet(session: AsyncSession, pointer: SidePointer, reason: str) -> None:
    """гасит всё ждущее в прохождении: просьбы, правки полей, файлы (Д50)"""
    pending_requests = list(
        await session.scalars(
            select(InteractionRequest).where(
                InteractionRequest.side_pointer_id == pointer.id,
                InteractionRequest.status == RequestStatus.PENDING,
            )
        )
    )
    for request in pending_requests:
        request.status = RequestStatus.CANCELLED
        request.decided_at = func.now()
        request.decision_comment = reason
        record(
            session,
            actor_id=None,
            event_type=AuditEventType.REQUEST_CANCELLED,
            target_type=TargetType.REQUEST,
            target_id=request.id,
            new_value={"reason": reason},
        )
    values_rows = list(
        await session.scalars(
            select(InteractionStageValues).where(
                InteractionStageValues.side_pointer_id == pointer.id,
                InteractionStageValues.pending_values.is_not(None),
            )
        )
    )
    for row in values_rows:
        row.pending_values = None
        row.pending_by = None
    docs = list(
        await session.scalars(
            select(InteractionDocument).where(
                InteractionDocument.side_pointer_id == pointer.id,
                InteractionDocument.status == DocumentStatus.PENDING,
            )
        )
    )
    for doc in docs:
        doc.status = DocumentStatus.REJECTED
        # отклонённая выходит из цепочки версий, как в document_service.decide
        doc.replaces_document_id = None


async def cancel_active(
    session: AsyncSession, scope: InteractionScope, reason: str
) -> None:
    pointer = await _active(session, scope.interaction.id)
    if pointer is not None:
        await cancel_locked(session, scope, pointer, reason)


async def listing(session: AsyncSession, interaction_id: int) -> list[SidePointer]:
    return list(
        await session.scalars(
            select(SidePointer)
            .where(SidePointer.interaction_id == interaction_id)
            .order_by(SidePointer.id.desc())
        )
    )
