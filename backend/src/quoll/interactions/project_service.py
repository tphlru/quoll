"""Операции над заявками. Каждая, кроме создания, начинается с захвата
области заявки под блокировкой - см. scope.py"""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.auth.keycloak_admin import verify_target
from quoll.auth.models import UserRole
from quoll.catalog.models import CloseLevel
from quoll.core.exceptions import (
    DomainRuleException,
    OperationForbiddenException,
    StaleStateException,
    WorkflowNotPublishedException,
)
from quoll.interactions import contract_service
from quoll.interactions.access_policy import (
    can_assign,
    can_cancel,
    can_close,
    can_pause,
)
from quoll.interactions.capacity_policy import (
    assert_can_keep_working,
    assert_can_take_new_work,
    counts_toward_capacity,
)
from quoll.interactions.close_reasons import check_reason
from quoll.interactions.models import (
    Branch,
    Interaction,
    InteractionAssignment,
    InteractionStageHistory,
    PauseState,
    SlotKind,
    StageChangeKind,
)
from quoll.interactions.notify import notify
from quoll.interactions.pause_policy import check_pause_term
from quoll.interactions.repository import InteractionRepository
from quoll.interactions.requests import cancel_pending_requests
from quoll.interactions.schemas import InteractionCreate, InteractionUpdate
from quoll.interactions.scope import InteractionScope, lock_interaction_scope
from quoll.interactions.transition_service import (
    lock_target_stage,
    place,
    reached_since_no_return,
)
from quoll.notifications import kinds
from quoll.workflows.graph_policy import edge_facts, reachable_from_start
from quoll.workflows.models import Stage, WorkflowTransition
from quoll.workflows.repository import WorkflowRepository


async def _assert_university_free(session: AsyncSession, university_id: int) -> None:
    """у вуза одна незакрытая заявка (М 4). Гонку ловит уникальный индекс"""
    if await session.scalar(
        select(Interaction.id).where(
            Interaction.university_id == university_id,
            Interaction.closed_at.is_(None),
        )
    ):
        raise DomainRuleException(
            409, "University already has an open interaction, add programs to it"
        )


async def create_interaction(
    session: AsyncSession, schema: InteractionCreate, author_id: str
) -> Interaction:
    """новая заявка рождается без владельца, черновиком автора - пока он
    её не назначит, другим руководителям она не видна"""
    await _assert_university_free(session, schema.university_id)
    if schema.workflow_id is not None:
        workflow = await WorkflowRepository(session).get(schema.workflow_id)
        if not workflow.is_published:
            raise WorkflowNotPublishedException(workflow.id)
    interaction = await InteractionRepository(session).create(
        schema.model_dump(exclude={"branches"}) | {"created_by": author_id}
    )
    for branch in schema.branches:
        await contract_service.draft_branch(
            session, interaction.id, branch.program_id, branch.product_id, author_id
        )
    return interaction


async def assign(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    manager_id: str,
    expected_owner_id: str | None,
    reason: str | None,
) -> Interaction:
    """назначить или переназначить заявку менеджеру"""
    # до блокировок: поход в сеть под блокировкой держал бы строки
    await verify_target(manager_id, UserRole.MANAGER)

    scope = await lock_interaction_scope(
        session, interaction_id, actor_id, target_manager_ids=[manager_id]
    )
    return await assign_locked(
        session,
        scope,
        manager_id=manager_id,
        expected_owner_id=expected_owner_id,
        reason=reason,
    )


async def assign_locked(
    session: AsyncSession,
    scope: InteractionScope,
    *,
    manager_id: str,
    expected_owner_id: str | None,
    reason: str | None,
    by_import: bool = False,
) -> Interaction:
    """назначение под уже захваченной областью - его зовёт и одобрение
    просьбы о передаче. Цель в Keycloak проверена до блокировок.

    by_import - перенос реестра админом: право руководителя не проверяется,
    остальные правила те же"""
    interaction = scope.interaction
    actor_id = scope.actor.id
    if interaction.owner_id != expected_owner_id:
        raise StaleStateException("Interaction owner", interaction.owner_id)

    target = scope.managers.get(manager_id)
    if target is None:
        raise DomainRuleException(400, f"User '{manager_id}' is not a manager")
    if not by_import and not can_assign(scope.actor, scope.ownership, target):
        raise OperationForbiddenException("assign this interaction")
    if manager_id == interaction.owner_id:
        raise DomainRuleException(
            400, "Interaction is already assigned to this manager"
        )

    stage = await _stage_of(session, interaction)
    if stage is not None and stage.is_terminal:
        raise DomainRuleException(409, "Closed interaction is reopened, not reassigned")
    # G8: у владельца заявки всегда есть руководитель
    if target.superviser_id is None:
        raise DomainRuleException(409, f"Manager '{manager_id}' has no supervisor")

    # пассивная у нового владельца тоже пассивна - места не просит
    delta = int(counts_toward_capacity(stage, interaction.is_paused, interaction.slot))
    await assert_can_take_new_work(session, target, delta)

    previous = interaction.owner_id
    interaction.owner_id = manager_id
    if previous is not None:
        interaction.last_owner_id = previous
    await _hand_over(session, interaction.id, manager_id, reason)
    await _announce_owner(session, scope, previous)
    # у нового владельца свои просьбы - старые устарели
    await cancel_pending_requests(
        session, interaction.id, actor_id, "interaction owner changed"
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.PROJECT_REASSIGNED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"owner_id": previous},
        new_value={"owner_id": manager_id, "reason": reason},
    )
    await session.flush()
    # updated_at ставит база - без refresh ответ полез бы за ним вне greenlet
    await session.refresh(interaction)
    return interaction


async def _stage_of(session: AsyncSession, interaction: Interaction) -> Stage | None:
    # связь state viewonly и ленивая - в async её не дёрнуть, читаем явно
    if interaction.state_id is None:
        return None
    return await session.get(Stage, interaction.state_id)


async def set_slot(
    session: AsyncSession,
    interaction: Interaction,
    slot: SlotKind,
    actor_id: str | None,
) -> None:
    """смена вида слота - в историю: отчёт за прошлый период её увидит"""
    interaction.slot = slot
    interaction.slot_changed_at = func.now()
    _event(
        session,
        interaction,
        StageChangeKind.SLOT_ACTIVE
        if slot == SlotKind.ACTIVE
        else StageChangeKind.SLOT_PASSIVE,
        actor_id,
        None,
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.INTERACTION_SLOT_CHANGED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        new_value={"slot": slot},
    )


def _event(
    session: AsyncSession,
    interaction: Interaction,
    kind: StageChangeKind,
    actor_id: str,
    comment: str | None,
) -> None:
    """событие истории без движения: заявка там же, где была"""
    session.add(
        InteractionStageHistory(
            interaction_id=interaction.id,
            from_stage_id=interaction.state_id,
            to_stage_id=interaction.state_id,
            kind=kind,
            actor_id=actor_id,
            comment=comment,
        )
    )


async def _release(session: AsyncSession, interaction_id: int) -> None:
    await session.execute(
        update(InteractionAssignment)
        .where(
            InteractionAssignment.interaction_id == interaction_id,
            InteractionAssignment.released_at.is_(None),
        )
        .values(released_at=func.now())
    )


async def _announce_owner(
    session: AsyncSession, scope: InteractionScope, previous: str | None
) -> None:
    """новому КАМу - «вам назначена», прежнему - «передали» (О 5)"""
    passive = scope.interaction.slot == SlotKind.PASSIVE
    await notify(
        session,
        kinds.INTERACTION_ASSIGNED,
        scope,
        context={"note": " (пассивная: в предел не входит)" if passive else ""},
    )
    if previous is not None:
        await notify(
            session, kinds.INTERACTION_TAKEN_AWAY, scope, previous_owner_id=previous
        )


async def _hand_over(
    session: AsyncSession, interaction_id: int, manager_id: str, reason: str | None
) -> None:
    """единственное место, где пишется история назначений: закрыть открытую
    запись и открыть новую. Открытая запись всегда совпадает с владельцем"""
    await _release(session, interaction_id)
    session.add(
        InteractionAssignment(
            interaction_id=interaction_id, manager_id=manager_id, reason=reason
        )
    )


async def decline(
    session: AsyncSession, *, interaction_id: int, actor_id: str, comment: str
) -> Interaction:
    """менеджер отказывается от ещё не принятой заявки - она возвращается
    черновиком к автору. От принятой отказываются просьбой о передаче"""
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if interaction.owner_id != actor_id:
        raise OperationForbiddenException("decline this interaction")
    if interaction.state_id is not None:
        raise DomainRuleException(
            409, "Accepted interaction is not declined, ask for a transfer"
        )
    interaction.owner_id = None
    interaction.last_owner_id = actor_id
    await _release(session, interaction.id)
    await cancel_pending_requests(
        session, interaction.id, actor_id, "interaction declined"
    )
    # причину видит автор: журнал читает только админ
    await notify(
        session, kinds.INTERACTION_DECLINED, scope, context={"comment": comment}
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.PROJECT_DECLINED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"owner_id": actor_id},
        new_value={"owner_id": None, "comment": comment},
    )
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def pause(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    until: datetime | None,
    comment: str,
) -> Interaction:
    """поставить на паузу или заменить паузу - продление и смена режима"""
    scope, _ = await _pausable(session, interaction_id, actor_id)
    interaction = scope.interaction
    if until is None and interaction.pause_state == PauseState.PAUSED_MANUAL:
        raise DomainRuleException(409, "Interaction is already paused without a term")
    if until is not None:
        check_pause_term(until)

    old_state = interaction.pause_state
    interaction.is_paused = True
    interaction.pause_state = (
        PauseState.PAUSED_TIMED if until is not None else PauseState.PAUSED_MANUAL
    )
    interaction.paused_until = until
    interaction.pause_comment = comment
    _event(session, interaction, StageChangeKind.PAUSE, actor_id, comment)
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.INTERACTION_PAUSED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"pause_state": old_state},
        new_value={
            "pause_state": interaction.pause_state,
            "until": until.isoformat() if until else None,
            "comment": comment,
        },
    )
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def unpause(
    session: AsyncSession, *, interaction_id: int, actor_id: str
) -> Interaction:
    scope, stage = await _pausable(session, interaction_id, actor_id)
    interaction = scope.interaction
    if not interaction.is_paused:
        raise DomainRuleException(409, "Interaction is not paused")
    if scope.owner is None:
        raise DomainRuleException(409, "Assign a manager before resuming")
    # слот возвращается, только если стадия его занимает
    delta = int(counts_toward_capacity(stage, False, interaction.slot)) - int(
        counts_toward_capacity(stage, True, interaction.slot)
    )
    await assert_can_keep_working(session, scope.owner, delta)
    await resume(session, interaction, actor_id)
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def resume(
    session: AsyncSession, interaction: Interaction, actor_id: str | None
) -> None:
    """снять паузу - руками или воркером по истечении срока. Ёмкость проверяет
    вызывающий, у него заблокирована строка менеджера"""
    old_state = interaction.pause_state
    interaction.is_paused = False
    interaction.pause_state = PauseState.ACTIVE
    interaction.paused_until = None
    interaction.pause_comment = None
    # после паузы застой считается заново - и у заявки, и у её веток (П8)
    interaction.stall_since = func.now()
    await session.execute(
        update(Branch)
        .where(
            Branch.interaction_id == interaction.id,
            Branch.state_id.is_not(None),
            Branch.closed_at.is_(None),
        )
        .values(stall_since=func.now())
    )
    _event(session, interaction, StageChangeKind.UNPAUSE, actor_id, None)
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.INTERACTION_UNPAUSED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"pause_state": old_state},
        new_value={"pause_state": PauseState.ACTIVE},
    )


async def _pausable(
    session: AsyncSession, interaction_id: int, actor_id: str
) -> tuple[InteractionScope, Stage]:
    """общее у паузы и снятия: права и стадия, на которой пауза имеет смысл"""
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if not can_pause(scope.actor, scope.ownership):
        raise OperationForbiddenException("pause this interaction")
    stage = await _stage_of(session, interaction)
    # пауза управляет слотом, а у черновика его нет
    if stage is None:
        raise DomainRuleException(409, "Draft without a stage cannot be paused")
    if stage.is_terminal:
        raise DomainRuleException(409, "Closed interaction cannot be paused")
    return scope, stage


async def update_settings(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    changes: dict,
) -> Interaction:
    """руководитель КАМа меняет пороги застоя и сроки предупреждений"""
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if not can_close(scope.actor, scope.ownership):
        raise OperationForbiddenException("change settings of this interaction")
    old = {
        "stall_overrides": dict(interaction.stall_overrides),
        "warn_days": interaction.warn_days,
    }
    if (overrides := changes.get("stall_overrides")) is not None:
        own = set(
            await session.scalars(
                select(Stage.id).where(Stage.workflow_id == interaction.workflow_id)
            )
        )
        if foreign := set(overrides) - own:
            raise DomainRuleException(
                400, f"Stages {sorted(foreign)} are not in this workflow"
            )
        merged = dict(interaction.stall_overrides)
        for stage_id, days in overrides.items():
            if days is None:
                merged.pop(str(stage_id), None)
            else:
                merged[str(stage_id)] = days
        interaction.stall_overrides = merged
    if "warn_days" in changes:
        interaction.warn_days = changes["warn_days"]
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.INTERACTION_SETTINGS_CHANGED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value=old,
        new_value={
            "stall_overrides": interaction.stall_overrides,
            "warn_days": interaction.warn_days,
        },
    )
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def update_fields(
    session: AsyncSession,
    interaction: Interaction,
    changes: InteractionUpdate,
    actor_id: str,
) -> Interaction:
    """описательные поля: права проверил роутер, блокировка не нужна -
    на них не опирается ни одно правило"""
    if interaction.closed_at is not None:
        raise DomainRuleException(409, "Interaction is closed")
    fields = changes.model_dump(exclude_unset=True)
    old = {name: getattr(interaction, name) for name in fields}
    updated = await InteractionRepository(session).update(interaction.id, changes)
    if fields:
        record(
            session,
            actor_id=actor_id,
            event_type=AuditEventType.INTERACTION_UPDATED,
            target_type=TargetType.INTERACTION,
            target_id=str(interaction.id),
            old_value=old,
            new_value=fields,
        )
    return updated


async def cancel_draft(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    close_reason_id: int,
    comment: str | None,
) -> Interaction:
    """черновик не удаляется, а отменяется (Р3): закрыт без стадии.
    Проверки - под блокировкой: черновик могли успеть поставить на стадию"""
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if not can_cancel(scope.actor, scope.ownership):
        raise OperationForbiddenException("cancel this interaction")
    if interaction.state_id is not None:
        raise DomainRuleException(409, "Only a draft is cancelled, close the rest")
    reason = await check_reason(
        session, close_reason_id, CloseLevel.INTERACTION_BEFORE_SIGNING, comment
    )
    interaction.closed_at = func.now()
    interaction.close_reason_id = reason.id
    _event(session, interaction, StageChangeKind.CANCEL, actor_id, comment)
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.INTERACTION_CANCELLED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"owner_id": interaction.owner_id},
        new_value={"reason": reason.code, "comment": comment},
    )
    await session.flush()
    await session.refresh(interaction)
    return interaction


async def reopen(
    session: AsyncSession,
    *,
    interaction_id: int,
    actor_id: str,
    manager_id: str,
    to_stage_id: int,
    expected_owner_id: str | None,
    comment: str,
) -> Interaction:
    """вернуть закрытую заявку в работу: владелец и стадия выбираются явно -
    за время простоя прежний мог уволиться или заполниться. Ребро не нужно,
    но стадия должна быть достижима из начальной - иначе тупик"""
    await verify_target(manager_id, UserRole.MANAGER)
    scope = await lock_interaction_scope(
        session,
        interaction_id,
        actor_id,
        target_manager_ids=[manager_id],
        allow_closed=True,
    )
    interaction = scope.interaction
    if interaction.owner_id != expected_owner_id:
        raise StaleStateException("Interaction owner", interaction.owner_id)
    target = scope.managers.get(manager_id)
    if target is None:
        raise DomainRuleException(400, f"User '{manager_id}' is not a manager")
    # права те же, что у назначения: иначе чужую закрытую забирали бы перебором id
    if not can_assign(scope.actor, scope.ownership, target):
        raise OperationForbiddenException("reopen this interaction")

    current = await _stage_of(session, interaction)
    if current is None or interaction.closed_at is None:
        raise DomainRuleException(409, "Only a closed interaction is reopened")
    await _assert_university_free(session, interaction.university_id)
    # флаги стадии неизменны (Р15) - проверяем до блокировки. Иначе запрос в
    # текущую закрытую стадию держал бы заявку на ней и ждал бы её саму, а
    # архивация этой стадии - наоборот
    requested = await session.get(Stage, to_stage_id)
    if requested is not None and (
        requested.is_terminal or requested.is_branch_stage or requested.is_side
    ):
        raise DomainRuleException(400, "Interaction is reopened into a working stage")
    stage = await lock_target_stage(session, to_stage_id)
    if stage.workflow_id != interaction.workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    edges = await session.scalars(
        select(WorkflowTransition).where(
            WorkflowTransition.workflow_id == stage.workflow_id,
            WorkflowTransition.is_active.is_(True),
        )
    )
    reachable = reachable_from_start(edge_facts(edges))
    if stage.id not in reachable:
        raise DomainRuleException(409, "Stage is not reachable from the start")
    # только туда, где заявка уже была: иначе переоткрытие перескочило бы
    # аппрувы и обязательные файлы, а после подписания - вернуло бы в первую часть
    if not await reached_since_no_return(session, interaction.id, stage.id):
        raise DomainRuleException(
            409, "Reopen goes only to a stage passed after the point of no return"
        )
    if target.superviser_id is None:
        raise DomainRuleException(409, f"Manager '{manager_id}' has no supervisor")
    await assert_can_take_new_work(
        # переоткрытая всегда активна - место считаем от нового вида слота
        session,
        target,
        int(counts_toward_capacity(stage, False, SlotKind.ACTIVE)),
    )

    previous_owner = interaction.owner_id
    if manager_id != previous_owner:
        # на прежнего - без новой записи: открытая запись и так его
        interaction.owner_id = manager_id
        if previous_owner is not None:
            interaction.last_owner_id = previous_owner
        await _hand_over(session, interaction.id, manager_id, comment)
        await _announce_owner(session, scope, previous_owner)
    await contract_service.reopen_branches(session, interaction.id, actor_id, comment)
    if interaction.slot != SlotKind.ACTIVE:
        await set_slot(session, interaction, SlotKind.ACTIVE, actor_id)
    place(
        session,
        interaction,
        current,
        stage,
        kind=StageChangeKind.REOPEN,
        transition_id=None,
        actor_id=actor_id,
        comment=comment,
    )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.PROJECT_REOPENED,
        target_type=TargetType.INTERACTION,
        target_id=interaction.id,
        old_value={"state_id": current.id, "owner_id": previous_owner},
        new_value={"state_id": stage.id, "owner_id": manager_id, "comment": comment},
    )
    await session.flush()
    await session.refresh(interaction)
    return interaction
