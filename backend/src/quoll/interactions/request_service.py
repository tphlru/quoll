"""Просьбы менеджера руководителю: передать проект, закрыть досрочно (П8),
пройти шаг с аппрувом - основным указателем или доп.

менеджер просит, руководитель владельца решает - кому передать, закрывать ли,
пускать ли дальше
"""

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.auth.keycloak_admin import verify_target
from quoll.auth.models import Manager, User, UserRole
from quoll.catalog.models import CloseLevel
from quoll.core.exceptions import (
    DomainRuleException,
    IdNotExistsException,
    OperationForbiddenException,
)
from quoll.core.locking import lock_row
from quoll.interactions import (
    branch_service,
    sa_service,
    side_pointer_service,
    step_hooks,
)
from quoll.interactions.access_policy import can_close
from quoll.interactions.close_reasons import check_reason, interaction_level
from quoll.interactions.models import (
    Branch,
    Interaction,
    InteractionRequest,
    InteractionStageHistory,
    RequestKind,
    RequestStatus,
    SidePointer,
    SidePointerStatus,
    StageChangeKind,
)
from quoll.interactions.notify import notify
from quoll.interactions.project_service import assign_locked
from quoll.interactions.repository import InteractionRepository
from quoll.interactions.scope import InteractionScope, lock_interaction_scope
from quoll.interactions.transition_service import (
    _check_contract_rules,
    check_step,
    close_locked,
    move_locked,
    return_locked,
    share_stage,
)
from quoll.notifications import kinds
from quoll.notifications.kinds import Subject
from quoll.workflows.models import Stage, WorkflowTransition

_REQUESTED = {
    RequestKind.TRANSFER: AuditEventType.PROJECT_TRANSFER_REQUESTED,
    RequestKind.CLOSE: AuditEventType.PROJECT_CLOSE_REQUESTED,
    RequestKind.TRANSITION: AuditEventType.TRANSITION_APPROVAL_REQUESTED,
}
_APPROVED = {
    RequestKind.TRANSFER: AuditEventType.PROJECT_TRANSFER_APPROVED,
    RequestKind.CLOSE: AuditEventType.PROJECT_CLOSE_APPROVED,
    RequestKind.TRANSITION: AuditEventType.TRANSITION_APPROVED,
}
_REJECTED = {
    RequestKind.TRANSFER: AuditEventType.PROJECT_TRANSFER_REJECTED,
    RequestKind.CLOSE: AuditEventType.PROJECT_CLOSE_REJECTED,
    RequestKind.TRANSITION: AuditEventType.TRANSITION_REJECTED,
}


@dataclass(frozen=True)
class Decision:
    request: InteractionRequest
    # просьба устарела и отменена - отказ, который надо закоммитить, а не откатить
    refused: str | None = None


async def _open_stage(session: AsyncSession, interaction: Interaction) -> Stage:
    """просят по заявке в работе: черновик удаляют, закрытую переоткрывают"""
    if interaction.state_id is None:
        raise DomainRuleException(409, "Draft interaction has no requests")
    stage = await session.get(Stage, interaction.state_id)
    if stage.is_terminal:
        raise DomainRuleException(409, "Interaction is already closed")
    return stage


async def _check_close_target(
    session: AsyncSession, interaction: Interaction, stage_id: int
) -> Stage:
    stage = await session.get(Stage, stage_id)
    if stage is None or stage.workflow_id != interaction.workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    if not stage.is_terminal or stage.is_branch_stage:
        raise DomainRuleException(400, "Interaction is closed into a terminal stage")
    return stage


async def create(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    kind: RequestKind,
    target_stage_id: int | None,
    target_manager_id: str | None,
    reason: str,
    branch_id: int | None = None,
    close_reason_id: int | None = None,
    branch_close_reason_id: int | None = None,
    side_pointer_id: int | None = None,
) -> InteractionRequest:
    if side_pointer_id is not None and (
        kind != RequestKind.TRANSITION or branch_id is not None
    ):
        raise DomainRuleException(400, "Side pass asks only for a transition")
    # под захватом области: просьба не создаётся одновременно со сменой владельца
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if interaction.owner_id != actor_id:
        raise OperationForbiddenException("request for someone else's interaction")
    current = await _open_stage(session, interaction)
    if side_pointer_id is not None:
        # доп. прохождение просит со своего шага, а не с шага заявки
        pointer = await side_pointer_service.load_active(
            session, interaction_id, side_pointer_id
        )
        current = await session.get(Stage, pointer.stage_id)

    transition_id = None
    # просьба уводит вперёд с шага обработчика (ДС) - он переходит в PENDING
    leaves_handler = False
    if branch_id is not None and kind == RequestKind.TRANSFER:
        raise DomainRuleException(400, "A branch is not transferred on its own")
    if kind == RequestKind.CLOSE and branch_id is not None:
        await branch_service.open_branch(session, scope, branch_id)
        # обоснование менеджера и есть комментарий к причине
        await check_reason(session, close_reason_id, CloseLevel.BRANCH, reason)
    elif kind == RequestKind.TRANSITION:
        if branch_id is not None:
            # шаг ветки: откуда просят - стадия ветки, не договора
            branch = await branch_service.open_branch(session, scope, branch_id)
            current = await session.get(Stage, branch.state_id)
        edge = await _approval_edge(session, interaction, current, target_stage_id)
        if branch_id is None and side_pointer_id is None:
            # невозвратное ребро без отметки «подписан» - отказ сейчас, а не
            # при одобрении
            await _check_contract_rules(
                session,
                interaction,
                current,
                edge,
                await session.get(Stage, target_stage_id),
                interaction.workflow_id,
            )
        # руководителю приходит готовый шаг: поля и файлы проверены сразу
        await check_step(
            session,
            interaction,
            current,
            edge,
            approved=True,
            branch_id=branch_id,
            side_pointer_id=side_pointer_id,
        )
        transition_id = edge.id
        leaves_handler = (
            branch_id is None and current.handler is not None and not edge.is_backward
        )
        if leaves_handler:
            await step_hooks.check_leave(session, scope, current, edge, side_pointer_id)
    elif kind == RequestKind.CLOSE:
        stage = await _check_close_target(session, interaction, target_stage_id)
        if stage.archived_at is not None:
            raise DomainRuleException(409, f"Stage '{stage.id}' is archived")
        await check_reason(
            session, close_reason_id, interaction_level(interaction), reason
        )
        if branch_close_reason_id is not None:
            await check_reason(
                session, branch_close_reason_id, CloseLevel.BRANCH, reason
            )
    elif (
        target_manager_id is not None
        and await session.get(Manager, target_manager_id) is None
    ):
        raise DomainRuleException(400, f"User '{target_manager_id}' is not a manager")

    pending = await session.scalar(
        select(InteractionRequest.id).where(
            InteractionRequest.interaction_id == interaction.id,
            InteractionRequest.kind == kind,
            InteractionRequest.branch_id.is_not_distinct_from(branch_id),
            InteractionRequest.side_pointer_id.is_not_distinct_from(side_pointer_id),
            InteractionRequest.status == RequestStatus.PENDING,
        )
    )
    if pending is not None:
        raise DomainRuleException(
            409, f"Request '{pending}' of this kind is already pending"
        )

    request = InteractionRequest(
        interaction_id=interaction.id,
        kind=kind,
        requested_by=actor_id,
        from_owner_id=interaction.owner_id,
        target_stage_id=target_stage_id,
        target_manager_id=target_manager_id,
        transition_id=transition_id,
        branch_id=branch_id,
        side_pointer_id=side_pointer_id,
        close_reason_id=close_reason_id,
        branch_close_reason_id=branch_close_reason_id,
        reason=reason,
    )
    session.add(request)
    await session.flush()
    if leaves_handler:
        await step_hooks.submitted(session, scope, current, side_pointer_id, actor_id)
    record(
        session,
        actor_id=actor_id,
        event_type=_REQUESTED[kind],
        target_type=TargetType.REQUEST,
        target_id=request.id,
        new_value={
            "interaction_id": interaction.id,
            "target_stage_id": target_stage_id,
            "target_manager_id": target_manager_id,
            "reason": reason,
        },
    )
    await notify(
        session,
        kinds.REQUEST_CREATED,
        scope,
        context={"request": _LABELS[kind], "reason": reason},
        subject=Subject.REQUEST,
        subject_id=request.id,
        payload={"request_id": request.id, "branch_id": branch_id},
    )
    await session.refresh(request)
    return request


_LABELS = {
    RequestKind.TRANSITION: "переход на следующий шаг",
    RequestKind.CLOSE: "закрытие",
    RequestKind.TRANSFER: "передача заявки",
}


async def _tell_requester(session, scope, request, decision: str, comment) -> None:
    await notify(
        session,
        kinds.REQUEST_DECIDED,
        scope,
        context={
            "decision": decision,
            "request": _LABELS[RequestKind(request.kind)],
            "comment": comment or "",
        },
        subject=Subject.REQUEST,
        subject_id=request.id,
        payload={"request_id": request.id, "branch_id": request.branch_id},
        requester_id=request.requested_by,
    )


async def _approval_edge(
    session: AsyncSession, interaction: Interaction, current: Stage, to_stage_id: int
) -> WorkflowTransition:
    edge = await session.scalar(
        select(WorkflowTransition).where(
            WorkflowTransition.workflow_id == interaction.workflow_id,
            WorkflowTransition.from_stage_id == current.id,
            WorkflowTransition.to_stage_id == to_stage_id,
            WorkflowTransition.is_active.is_(True),
        )
    )
    if edge is None or not edge.requires_approval:
        raise DomainRuleException(400, "No transition needing approval to this stage")
    return edge


async def _from_handler(session: AsyncSession, request: InteractionRequest) -> bool:
    """просьба о выходе с шага обработчика (ДС) - основного или доп. прохождения"""
    if request.kind != RequestKind.TRANSITION or request.branch_id is not None:
        return False
    edge = await session.get(WorkflowTransition, request.transition_id)
    return (await session.get(Stage, edge.from_stage_id)).handler is not None


async def _pre_share(session: AsyncSession, found: InteractionRequest) -> set[int]:
    """стадии, от которых зависит решение, - без блокировок: их FOR SHARE
    берётся раньше области заявки (см. share_stage)"""
    if found.kind != RequestKind.TRANSITION:
        return set()
    shared: set[int] = set()
    if found.branch_id is not None or found.side_pointer_id is not None:
        edge = await session.get(WorkflowTransition, found.transition_id)
        shared |= {found.target_stage_id, edge.reject_to_stage_id} - {None}
    if await _from_handler(session, found):
        shared |= await step_hooks.pre_share(
            session, found.interaction_id, found.side_pointer_id, found.target_stage_id
        )
    return shared


async def _lock_for_decision(
    session: AsyncSession,
    request_id: int,
    actor_id: str,
    target_manager_ids: list[str] = (),
    shared: set[int] = frozenset(),
) -> tuple[InteractionScope, InteractionRequest]:
    """заявка раньше просьбы - порядок из core/locking.py. Стадии, куда
    заявка/ветка/доп. указатель пойдёт, - раньше заявки (см. share_stage)"""
    found = await session.get(InteractionRequest, request_id)
    if found is None:
        raise IdNotExistsException(InteractionRequest.__name__)
    for stage_id in sorted(shared):
        await share_stage(session, stage_id)
    # по закрытой решение не падает, а гасит просьбу как устаревшую
    scope = await lock_interaction_scope(
        session, found.interaction_id, actor_id, target_manager_ids, allow_closed=True
    )
    request = await lock_row(session, InteractionRequest, request_id)
    if request.status != RequestStatus.PENDING:
        raise DomainRuleException(409, f"Request is already {request.status}")
    if not can_close(scope.actor, scope.ownership):
        raise OperationForbiddenException("decide on this request")
    return scope, request


def _decide(
    request: InteractionRequest,
    status: RequestStatus,
    actor_id: str | None,
    comment: str | None,
) -> None:
    request.status = status
    request.decided_by = actor_id
    request.decided_at = func.now()
    request.decision_comment = comment


async def _stale_reason(
    session: AsyncSession, scope: InteractionScope, request: InteractionRequest
) -> str | None:
    """просьба сама стала недействительной - это не ошибка руководителя"""
    interaction = scope.interaction
    if interaction.owner_id != request.from_owner_id:
        return "interaction owner changed"
    if interaction.closed_at is not None:
        return "interaction closed"
    if request.kind == RequestKind.CLOSE and request.branch_id is not None:
        branch = await session.get(Branch, request.branch_id)
        return "branch closed" if branch.closed_at is not None else None
    if request.kind == RequestKind.TRANSITION:
        edge = await session.get(WorkflowTransition, request.transition_id)
        if request.side_pointer_id is not None:
            # основной указатель к доп. прохождению отношения не имеет
            pointer = await session.get(SidePointer, request.side_pointer_id)
            if pointer.status != SidePointerStatus.ACTIVE:
                return "side pointer finished"
            if pointer.stage_id != edge.from_stage_id:
                return "side pointer moved"
        elif request.branch_id is not None:
            branch = await session.get(Branch, request.branch_id)
            if branch.closed_at is not None:
                return "branch closed"
            if branch.state_id != edge.from_stage_id:
                return "branch moved"
        elif interaction.state_id != edge.from_stage_id:
            return "interaction moved"
        if not edge.is_active:
            return "transition deactivated"
        if not edge.is_backward and await _from_handler(session, request):
            sa = await sa_service.open_of_pass(
                session, interaction.id, request.side_pointer_id
            )
            if sa is None:
                return "agreement is not open"
            return await sa_service.stale_actions(session, interaction, sa.id)
    if request.kind == RequestKind.CLOSE:
        # FOR SHARE: архивация, начатая раньше, закоммитится, и мы увидим
        # архив здесь, а не упадём позже в закрытии - просьба тогда осталась
        # бы висеть вместо отмены
        target = (
            await session.execute(
                select(Stage)
                .where(Stage.id == request.target_stage_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if target.archived_at is not None:
            return "target stage archived"
    return None


async def approve(
    session: AsyncSession,
    *,
    request_id: int,
    actor_id: str,
    target_manager_id: str | None,
    comment: str | None,
) -> Decision:
    """ошибки по цели, которую выбрал руководитель, - 409, просьба ждёт дальше.
    Устаревшая просьба отменяется, и отмена коммитится"""
    found = await session.get(InteractionRequest, request_id)
    if found is None:
        raise IdNotExistsException(InteractionRequest.__name__)
    # предварительно, без блокировок: чужому руководителю - 403, решённой -
    # 409, и Keycloak не дёргаем зря. Под блокировкой проверится ещё раз
    if found.status != RequestStatus.PENDING:
        raise DomainRuleException(409, f"Request is already {found.status}")
    repo = InteractionRepository(session)
    actor = await session.get(User, actor_id)
    ownership = await repo.ownership(await repo.get(found.interaction_id))
    if not can_close(actor, ownership):
        raise OperationForbiddenException("decide on this request")
    if found.kind == RequestKind.TRANSFER:
        if target_manager_id is None:
            raise DomainRuleException(422, "Transfer approval needs target_manager_id")
        # до блокировок: поход в сеть под блокировкой держал бы строки
        await verify_target(target_manager_id, UserRole.MANAGER)
    shared = await _pre_share(session, found)
    scope, request = await _lock_for_decision(
        session,
        request_id,
        actor_id,
        [target_manager_id] if target_manager_id else [],
        shared,
    )

    stale = await _stale_reason(session, scope, request)
    if stale is not None:
        _decide(request, RequestStatus.CANCELLED, actor_id, stale)
        if await _from_handler(session, request):
            await step_hooks.returned(
                session,
                scope,
                request.side_pointer_id,
                stale,
                StageChangeKind.SA_RETURNED,
                actor_id,
            )
        record(
            session,
            actor_id=actor_id,
            event_type=AuditEventType.REQUEST_CANCELLED,
            target_type=TargetType.REQUEST,
            target_id=request.id,
            new_value={"reason": stale},
        )
        await _tell_requester(session, scope, request, "отменена", stale)
        await session.flush()
        return Decision(request, refused=f"Request is no longer valid: {stale}")

    # решаем раньше операции: она отменяет открытые просьбы по заявке, и эта
    # попала бы под отмену. Упадёт операция - откатится и решение
    _decide(request, RequestStatus.APPROVED, actor_id, comment)
    await session.flush()
    if request.kind == RequestKind.TRANSFER:
        await assign_locked(
            session,
            scope,
            manager_id=target_manager_id,
            expected_owner_id=request.from_owner_id,
            reason=request.reason,
        )
        outcome = {
            "target_manager_id": target_manager_id,
            "suggested": request.target_manager_id,
        }
    elif request.kind == RequestKind.TRANSITION and request.side_pointer_id is not None:
        # ACTIVE и на месте - проверено в _stale_reason
        pointer = await session.get(SidePointer, request.side_pointer_id)
        await side_pointer_service.move_locked(
            session,
            scope,
            pointer,
            request.target_stage_id,
            comment or request.reason,
            approved=True,
            shared=shared,
        )
        outcome = {"target_stage_id": request.target_stage_id}
    elif request.kind == RequestKind.TRANSITION and request.branch_id is not None:
        branch = await branch_service.open_branch(session, scope, request.branch_id)
        await branch_service.move_locked(
            session,
            scope,
            branch,
            to_stage_id=request.target_stage_id,
            comment=comment or request.reason,
            approved=True,
        )
        outcome = {"target_stage_id": request.target_stage_id}
    elif request.kind == RequestKind.TRANSITION:
        await move_locked(
            session,
            scope,
            to_stage_id=request.target_stage_id,
            comment=comment or request.reason,
            approved=True,
            shared=shared,
        )
        outcome = {"target_stage_id": request.target_stage_id}
    elif request.branch_id is not None:
        branch = await branch_service.open_branch(session, scope, request.branch_id)
        await branch_service.close_locked(
            session,
            scope,
            branch,
            close_reason_id=request.close_reason_id,
            comment=request.reason,
        )
        outcome = {"branch_id": request.branch_id}
    else:
        await close_locked(
            session,
            scope,
            to_stage_id=request.target_stage_id,
            close_reason_id=request.close_reason_id,
            branch_close_reason_id=request.branch_close_reason_id,
            comment=request.reason,
        )
        outcome = {"target_stage_id": request.target_stage_id}

    record(
        session,
        actor_id=actor_id,
        event_type=_APPROVED[request.kind],
        target_type=TargetType.REQUEST,
        target_id=request.id,
        new_value={**outcome, "comment": comment},
    )
    await _tell_requester(session, scope, request, "одобрена", comment)
    await session.flush()
    await session.refresh(request)
    return Decision(request)


async def reject(
    session: AsyncSession, *, request_id: int, actor_id: str, comment: str
) -> InteractionRequest:
    found = await session.get(InteractionRequest, request_id)
    if found is None:
        raise IdNotExistsException(InteractionRequest.__name__)
    scope, request = await _lock_for_decision(
        session, request_id, actor_id, shared=await _pre_share(session, found)
    )
    _decide(request, RequestStatus.REJECTED, actor_id, comment)
    if await _from_handler(session, request):
        # ДС - снова черновик до того, как отказ, возможно, уведёт с шага
        await step_hooks.returned(
            session,
            scope,
            request.side_pointer_id,
            comment,
            StageChangeKind.SA_REJECTED,
            actor_id,
        )
    if request.kind == RequestKind.TRANSITION and request.side_pointer_id is not None:
        await _send_back_side(session, scope, request, comment)
    elif request.kind == RequestKind.TRANSITION:
        await _send_back(session, scope, request, comment)
    record(
        session,
        actor_id=actor_id,
        event_type=_REJECTED[request.kind],
        target_type=TargetType.REQUEST,
        target_id=request.id,
        new_value={"comment": comment},
    )
    await _tell_requester(session, scope, request, "отклонена", comment)
    await session.flush()
    await session.refresh(request)
    return request


async def _send_back(
    session: AsyncSession,
    scope: InteractionScope,
    request: InteractionRequest,
    comment: str,
) -> None:
    """отказ в аппруве уводит на доработку, если ребро это задаёт и заявка
    всё ещё там, откуда просили. Повторный проход снова потребует аппрува"""
    edge = await session.get(WorkflowTransition, request.transition_id)
    if edge.reject_to_stage_id is None:
        return
    target = await session.get(Stage, edge.reject_to_stage_id)
    if target is None or target.archived_at is not None:
        return
    await session.flush()
    if request.branch_id is not None:
        branch = await session.get(Branch, request.branch_id)
        if branch.closed_at is None and branch.state_id == edge.from_stage_id:
            await branch_service.return_locked(
                session,
                scope,
                branch,
                to_stage_id=target.id,
                kind=StageChangeKind.REJECTION,
                comment=comment,
            )
        return
    if scope.interaction.state_id != edge.from_stage_id:
        return
    await return_locked(
        session,
        scope,
        to_stage_id=target.id,
        kind=StageChangeKind.REJECTION,
        comment=comment,
    )


async def _send_back_side(
    session: AsyncSession,
    scope: InteractionScope,
    request: InteractionRequest,
    comment: str,
) -> None:
    """отказ доп. прохождению: на reject_to, если он доп. и указатель там же,
    иначе указатель остаётся на шаге (§6.7)"""
    edge = await session.get(WorkflowTransition, request.transition_id)
    if edge.reject_to_stage_id is None:
        return
    target = await session.get(Stage, edge.reject_to_stage_id)
    if target is None or target.archived_at is not None or not target.is_side:
        return
    pointer = await session.get(SidePointer, request.side_pointer_id)
    if (
        pointer.status != SidePointerStatus.ACTIVE
        or pointer.stage_id != edge.from_stage_id
    ):
        return
    actor_id = scope.actor.id
    source = await session.get(Stage, pointer.stage_id)
    await step_hooks.abandon(session, scope, source, pointer.id, comment, actor_id)
    pointer.stage_id = target.id
    session.add(
        InteractionStageHistory(
            interaction_id=scope.interaction.id,
            side_pointer_id=pointer.id,
            from_stage_id=source.id,
            to_stage_id=target.id,
            transition_id=edge.id,
            kind=StageChangeKind.REJECTION,
            actor_id=actor_id,
            comment=comment,
        )
    )
    await step_hooks.enter(session, scope, target, pointer.id, actor_id)


async def withdraw(session: AsyncSession, *, request_id: int, actor_id: str) -> None:
    """автор отзывает сам: CAS одной строки. Заявку блокируем, только если
    просьба с шага обработчика - отзыв возвращает ДС в черновик"""
    found = await session.get(InteractionRequest, request_id)
    if found is None:
        raise IdNotExistsException(InteractionRequest.__name__)
    scope = None
    if await _from_handler(session, found):
        scope = await lock_interaction_scope(session, found.interaction_id, actor_id)
    withdrawn = await session.scalar(
        update(InteractionRequest)
        .where(
            InteractionRequest.id == request_id,
            InteractionRequest.requested_by == actor_id,
            InteractionRequest.status == RequestStatus.PENDING,
        )
        .values(
            status=RequestStatus.CANCELLED,
            decided_by=actor_id,
            decided_at=func.now(),
            decision_comment="withdrawn by author",
        )
        .returning(InteractionRequest.id)
    )
    if withdrawn is None:
        request = await session.get(
            InteractionRequest, request_id, populate_existing=True
        )
        if request.requested_by != actor_id:
            raise OperationForbiddenException("withdraw someone else's request")
        raise DomainRuleException(409, f"Request is already {request.status}")
    if scope is not None:
        await step_hooks.returned(
            session,
            scope,
            found.side_pointer_id,
            "withdrawn by author",
            StageChangeKind.SA_RETURNED,
            actor_id,
        )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.REQUEST_CANCELLED,
        target_type=TargetType.REQUEST,
        target_id=request_id,
        new_value={"reason": "withdrawn by author"},
    )


async def visible(
    session: AsyncSession,
    viewer: User,
    status: RequestStatus | None,
    limit: int,
    offset: int,
) -> list[InteractionRequest]:
    """руководитель - по заявкам своей команды, менеджер - свои, админ - все"""
    stmt = select(InteractionRequest)
    if viewer.role == UserRole.MANAGER:
        stmt = stmt.where(InteractionRequest.requested_by == viewer.id)
    elif viewer.role == UserRole.SUPERVISER:
        team = select(Manager.id).where(Manager.superviser_id == viewer.id)
        owned = select(Interaction.id).where(Interaction.owner_id.in_(team))
        stmt = stmt.where(InteractionRequest.interaction_id.in_(owned))
    if status is not None:
        stmt = stmt.where(InteractionRequest.status == status)
    stmt = (
        stmt.order_by(
            InteractionRequest.created_at.desc(), InteractionRequest.id.desc()
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.scalars(stmt)).all())
