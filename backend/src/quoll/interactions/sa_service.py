"""Допсоглашение (шаг 4.1, М 3.13, 6.1): данные прохождения обработчика
SUPPLEMENTARY_AGREEMENT. Открытие, отправка и применение идут через хуки шага
(step_hooks) - здесь только правила ДС и его действий.

своих блокировок у ДС и действий нет - их прикрывает взаимодействие
"""

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit_models import AuditEventType
from quoll.core.exceptions import (
    DomainRuleException,
    IdNotExistsException,
    OperationForbiddenException,
)
from quoll.interactions import sa_lifecycle
from quoll.interactions.access_policy import can_change
from quoll.interactions.bindings import check_signed_date, current_contract
from quoll.interactions.contract_service import check_pair
from quoll.interactions.document_service import current_scan
from quoll.interactions.models import (
    ActionType,
    AgreementAction,
    AgreementStatus,
    Branch,
    Interaction,
    InteractionRequest,
    RequestStatus,
    StageChangeKind,
    SupplementaryAgreement,
)
from quoll.interactions.scope import InteractionScope, lock_interaction_scope
from quoll.workflows.models import Stage, WorkflowTransition
from quoll.workflows.step_handlers import SUPPLEMENTARY_AGREEMENT

OPEN = (AgreementStatus.DRAFT, AgreementStatus.PENDING)
WITH_BRANCH = (ActionType.EXTEND_LICENSE, ActionType.RESUME, ActionType.EXCLUDE)


# --- общее


async def handler_stage(session: AsyncSession, workflow_id: int | None) -> Stage | None:
    return await session.scalar(
        select(Stage)
        .where(
            Stage.workflow_id == workflow_id,
            Stage.handler == SUPPLEMENTARY_AGREEMENT,
            Stage.archived_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )


async def branch_start(session: AsyncSession, workflow_id: int | None) -> Stage | None:
    return await session.scalar(
        select(Stage)
        .where(
            Stage.workflow_id == workflow_id,
            Stage.is_branch_start.is_(True),
            Stage.archived_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )


async def load(
    session: AsyncSession, interaction: Interaction, sa_id: int
) -> SupplementaryAgreement:
    sa = await session.get(SupplementaryAgreement, sa_id, populate_existing=True)
    if sa is None or sa.interaction_id != interaction.id:
        raise DomainRuleException(
            404, "Supplementary agreement is not in this interaction"
        )
    return sa


async def open_of_pass(
    session: AsyncSession, interaction_id: int, pass_id: int | None
) -> SupplementaryAgreement | None:
    """незавершённое ДС этого прохождения - или None, если его нет"""
    return await session.scalar(
        select(SupplementaryAgreement)
        .where(
            SupplementaryAgreement.interaction_id == interaction_id,
            SupplementaryAgreement.side_pointer_id.is_not_distinct_from(pass_id),
            SupplementaryAgreement.status.in_(OPEN),
        )
        .execution_options(populate_existing=True)
    )


async def stage_of(session: AsyncSession, sa: SupplementaryAgreement) -> Stage | None:
    interaction = await session.get(Interaction, sa.interaction_id)
    return await handler_stage(session, interaction.workflow_id)


async def is_forward_exit(
    session: AsyncSession, stage: Stage, to_stage_id: int
) -> bool:
    return bool(
        await session.scalar(
            select(WorkflowTransition.id).where(
                WorkflowTransition.from_stage_id == stage.id,
                WorkflowTransition.to_stage_id == to_stage_id,
                WorkflowTransition.is_active.is_(True),
                WorkflowTransition.is_backward.is_(False),
            )
        )
    )


def require_draft(sa: SupplementaryAgreement) -> None:
    if sa.status != AgreementStatus.DRAFT:
        raise DomainRuleException(
            409, f"Supplementary agreement is {sa.status}, only a draft is changed"
        )


def require_change(scope: InteractionScope) -> None:
    if not can_change(scope.actor, scope.ownership):
        raise OperationForbiddenException("change agreements of this interaction")


async def actions_of(session: AsyncSession, sa_id: int) -> list[AgreementAction]:
    return list(
        await session.scalars(
            select(AgreementAction)
            .where(AgreementAction.sa_id == sa_id)
            .order_by(AgreementAction.id)
        )
    )


async def _live_of_pair(
    session: AsyncSession, interaction_id: int, program_id, product_id
) -> Branch | None:
    return await session.scalar(
        select(Branch).where(
            Branch.interaction_id == interaction_id,
            Branch.program_id.is_not_distinct_from(program_id),
            Branch.product_id.is_not_distinct_from(product_id),
            Branch.closed_at.is_(None),
        )
    )


# --- проверка действия (одна на добавление, отправку и одобрение)


async def action_problem(
    session: AsyncSession,
    interaction: Interaction,
    action: AgreementAction,
    others: list[AgreementAction],
) -> str | None:
    """что мешает действию; others - остальные действия этого ДС"""
    branch = None
    if action.type in WITH_BRANCH:
        branch = await session.get(Branch, action.branch_id, populate_existing=True)
        if branch is None or branch.interaction_id != interaction.id:
            return f"branch {action.branch_id} is not in this interaction"
        if branch.state_id is None:
            return f"branch {branch.id} is a draft, it is not changed by an agreement"
        same = [o for o in others if o.branch_id == branch.id]
        if any(o.type == ActionType.EXCLUDE for o in same):
            return f"branch {branch.id} is excluded by this agreement"
        if action.type == ActionType.EXCLUDE and same:
            return f"exclusion is the only action for branch {branch.id}"
    handler = {
        ActionType.NEW_BRANCH: _new_branch_problem,
        ActionType.EXTEND_LICENSE: _extend_license_problem,
        ActionType.RESUME: _resume_problem,
        ActionType.EXCLUDE: _exclude_problem,
        ActionType.EXTEND_CONTRACT: _extend_contract_problem,
    }[ActionType(action.type)]
    return await handler(session, interaction, action, others, branch)


async def _new_branch_problem(session, interaction, action, others, _branch):
    try:
        await check_pair(session, action.program_id, action.product_id)
    except DomainRuleException as err:
        return err.message
    live = await _live_of_pair(
        session, interaction.id, action.program_id, action.product_id
    )
    if live is not None and live.state_id is not None:
        return f"branch {live.id} of this program and product is open"
    for other in others:
        if other.type == ActionType.RESUME:
            resumed = await session.get(Branch, other.branch_id)
            if (resumed.program_id, resumed.product_id) == (
                action.program_id,
                action.product_id,
            ):
                return "resume or a new iteration, not both"
    if await branch_start(session, interaction.workflow_id) is None:
        return "workflow has no branch start"
    return None


async def _extend_license_problem(session, interaction, action, others, branch):
    resumed = any(
        o.type == ActionType.RESUME and o.branch_id == branch.id for o in others
    )
    if branch.closed_at is not None and not resumed:
        return f"branch {branch.id} is closed"
    if branch.license_until is None:
        return f"branch {branch.id} has no license yet, fill it on step 5"
    if action.license_until <= branch.license_until:
        return f"new term must be later than {branch.license_until.isoformat()}"
    signed = branch.license_signed_at
    if signed is not None and action.license_until <= signed:
        return "term must be after signing"
    return None


async def _resume_problem(session, interaction, action, others, branch):
    if branch.closed_at is None:
        return f"branch {branch.id} is open"
    if branch.closed_with_interaction:
        return f"branch {branch.id} returns with the interaction"
    live = await _live_of_pair(
        session, interaction.id, branch.program_id, branch.product_id
    )
    if live is not None:
        return f"branch {live.id} of this pair is live"
    # возобновляется только последняя итерация пары (Д21) - заодно двух
    # RESUME одной пары в одном ДС не бывает
    latest = await session.scalar(
        select(func.max(Branch.iteration)).where(
            Branch.interaction_id == interaction.id,
            Branch.program_id.is_not_distinct_from(branch.program_id),
            Branch.product_id.is_not_distinct_from(branch.product_id),
        )
    )
    if branch.iteration < latest:
        return f"only the latest iteration {latest} of this pair is resumed"
    if any(
        o.type == ActionType.NEW_BRANCH
        and (o.program_id, o.product_id) == (branch.program_id, branch.product_id)
        for o in others
    ):
        return "resume or a new iteration, not both"
    stage = await session.get(Stage, branch.state_id, populate_existing=True)
    # с терминальной ветка уходит на начало веток - её архив не мешает
    if stage.archived_at is not None and not stage.is_terminal:
        return f"branch {branch.id} step is archived"
    if (
        stage.is_terminal
        and await branch_start(session, interaction.workflow_id) is None
    ):
        return "workflow has no branch start"
    return None


async def _exclude_problem(session, interaction, action, others, branch):
    if branch.closed_at is not None:
        return f"branch {branch.id} is closed"
    return None


async def _extend_contract_problem(session, interaction, action, others, _branch):
    contract = await current_contract(session, interaction.id)
    if contract is None:
        return "no current contract document"
    new = action.contract_valid_until
    if contract.contract_signed_at is not None and new <= contract.contract_signed_at:
        return "term must be after signing"
    if (
        contract.contract_valid_until is not None
        and new <= contract.contract_valid_until
    ):
        return (
            f"new term must be later than {contract.contract_valid_until.isoformat()}"
        )
    return None


# --- черновик


async def update(
    session: AsyncSession,
    *,
    interaction_id: int,
    sa_id: int,
    actor_id: str,
    changes: dict[str, Any],
) -> SupplementaryAgreement:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    require_change(scope)
    sa = await load(session, scope.interaction, sa_id)
    require_draft(sa)
    if changes.get("signed_at") is not None:
        check_signed_date(changes["signed_at"])
    old = {k: _plain(getattr(sa, k)) for k in changes}
    for key, value in changes.items():
        setattr(sa, key, value)
    sa_lifecycle.journal(
        session,
        actor_id,
        AuditEventType.SA_UPDATED,
        sa,
        old,
        {k: _plain(v) for k, v in changes.items()},
    )
    await session.flush()
    await session.refresh(sa)
    return sa


def _plain(value):
    return value.isoformat() if isinstance(value, date) else value


async def add_action(
    session: AsyncSession,
    *,
    interaction_id: int,
    sa_id: int,
    actor_id: str,
    fields: dict[str, Any],
) -> AgreementAction:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    require_change(scope)
    sa = await load(session, scope.interaction, sa_id)
    require_draft(sa)
    action = AgreementAction(sa_id=sa.id, **fields)
    others = await actions_of(session, sa.id)
    if problem := await action_problem(session, scope.interaction, action, others):
        raise DomainRuleException(409, problem)
    session.add(action)
    await session.flush()
    sa_lifecycle.journal(
        session,
        actor_id,
        AuditEventType.SA_ACTION_ADDED,
        sa,
        new={"action_id": action.id, **{k: _plain(v) for k, v in fields.items()}},
    )
    await session.refresh(action)
    return action


async def remove_action(
    session: AsyncSession,
    *,
    interaction_id: int,
    sa_id: int,
    action_id: int,
    actor_id: str,
) -> None:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    require_change(scope)
    sa = await load(session, scope.interaction, sa_id)
    require_draft(sa)
    action = await session.get(AgreementAction, action_id)
    if action is None or action.sa_id != sa.id:
        raise DomainRuleException(404, "Action is not in this agreement")
    await session.delete(action)
    await session.flush()
    sa_lifecycle.journal(
        session,
        actor_id,
        AuditEventType.SA_ACTION_REMOVED,
        sa,
        old={"action_id": action_id},
    )


# --- чтение


async def view(session: AsyncSession, sa: SupplementaryAgreement) -> dict[str, Any]:
    scan = await current_scan(session, sa.id)
    # ждущий выход этого прохождения с шага ДС
    pending = await session.scalar(
        select(InteractionRequest.id)
        .join(
            WorkflowTransition,
            WorkflowTransition.id == InteractionRequest.transition_id,
        )
        .join(Stage, Stage.id == WorkflowTransition.from_stage_id)
        .where(
            InteractionRequest.interaction_id == sa.interaction_id,
            InteractionRequest.side_pointer_id.is_not_distinct_from(sa.side_pointer_id),
            InteractionRequest.status == RequestStatus.PENDING,
            Stage.handler == SUPPLEMENTARY_AGREEMENT,
        )
    )
    return {
        "id": sa.id,
        "interaction_id": sa.interaction_id,
        "side_pointer_id": sa.side_pointer_id,
        "number": sa.number,
        "signed_at": sa.signed_at,
        "status": sa.status,
        "created_by": sa.created_by,
        "created_at": sa.created_at,
        "decided_by": sa.decided_by,
        "decided_at": sa.decided_at,
        "decision_comment": sa.decision_comment,
        "scan_document_id": scan.id if scan else None,
        "pending_request_id": pending,
        "actions": await actions_of(session, sa.id),
    }


async def listing(session: AsyncSession, interaction_id: int) -> list[dict[str, Any]]:
    agreements = await session.scalars(
        select(SupplementaryAgreement)
        .where(SupplementaryAgreement.interaction_id == interaction_id)
        .order_by(SupplementaryAgreement.id.desc())
    )
    return [await view(session, sa) for sa in agreements]


async def upload_scan(
    session: AsyncSession,
    attachments,
    *,
    interaction_id: int,
    sa_id: int,
    actor,
    file,
    replaces_document_id: int | None,
):
    """скан - своей кнопкой: вид и шаг ставятся сами (М 3.14)"""
    from quoll.interactions.document_service import DocumentFields, upload

    interaction = await session.get(Interaction, interaction_id)
    if interaction is None:
        raise IdNotExistsException(Interaction.__name__)
    sa = await load(session, interaction, sa_id)
    step = await handler_stage(session, interaction.workflow_id)
    if step is None:
        raise DomainRuleException(409, "Workflow has no supplementary agreement step")
    return await upload(
        session,
        attachments,
        interaction_id=interaction_id,
        actor=actor,
        file=file,
        stage_id=step.id,
        replaces_document_id=replaces_document_id,
        fields=DocumentFields(kind="SUPPLEMENTARY_AGREEMENT"),
        supplementary_agreement_id=sa_id,
        side_pointer_id=sa.side_pointer_id,
    )


# --- одобрение


async def stages_to_share(
    session: AsyncSession, sa: SupplementaryAgreement
) -> set[int]:
    """стадии, от которых зависит одобрение: берутся FOR SHARE до области
    заявки (§4). Читаем без блокировок - под областью сверим"""
    interaction = await session.get(Interaction, sa.interaction_id)
    stages: set[int] = set()
    need_start = False
    for action in await actions_of(session, sa.id):
        if action.type == ActionType.NEW_BRANCH:
            need_start = True
        elif action.type == ActionType.RESUME:
            branch = await session.get(Branch, action.branch_id)
            stages.add(branch.state_id)
            if (await session.get(Stage, branch.state_id)).is_terminal:
                need_start = True
    if need_start:
        start = await branch_start(session, interaction.workflow_id)
        if start is not None:
            stages.add(start.id)
    return stages


async def problems(
    session: AsyncSession, interaction: Interaction, sa: SupplementaryAgreement
) -> list[str]:
    """что мешает прямому выходу с шага (скан, каждое действие)"""
    found = []
    if await current_scan(session, sa.id) is None:
        found.append("scan is missing")
    actions = await actions_of(session, sa.id)
    for action in actions:
        others = [a for a in actions if a.id != action.id]
        if problem := await action_problem(session, interaction, action, others):
            found.append(f"action {action.id}: {problem}")
    return found


def submit(session: AsyncSession, sa: SupplementaryAgreement, actor_id: str) -> None:
    """создана просьба TRANSITION с шага ДС - DRAFT -> PENDING"""
    require_draft(sa)
    sa.status = AgreementStatus.PENDING
    sa_lifecycle.journal(session, actor_id, AuditEventType.SA_SUBMITTED, sa)


async def stale_actions(
    session: AsyncSession, interaction: Interaction, sa_id: int
) -> str | None:
    """почему одобрять уже нельзя: руководитель не виноват, просьба гасится"""
    sa = await session.get(SupplementaryAgreement, sa_id, populate_existing=True)
    if sa.status != AgreementStatus.PENDING:
        return f"agreement is {sa.status}"
    if await current_scan(session, sa.id) is None:
        return "scan is missing"
    actions = await actions_of(session, sa.id)
    for action in actions:
        others = [a for a in actions if a.id != action.id]
        if problem := await action_problem(session, interaction, action, others):
            return f"action {action.id}: {problem}"
    return None


_ORDER = {
    ActionType.NEW_BRANCH: 0,
    ActionType.RESUME: 1,
    ActionType.EXTEND_LICENSE: 2,
    ActionType.EXTEND_CONTRACT: 3,
    # последним: если закроет последнюю ветку, ALL_BRANCHES_CLOSED не будет ложным
    ActionType.EXCLUDE: 4,
}


async def apply(
    session: AsyncSession,
    scope: InteractionScope,
    sa: SupplementaryAgreement,
    actor_id: str | None,
    comment: str | None,
    shared: set[int],
) -> None:
    """одобрение: все действия одной транзакцией (I5). shared - стадии, на
    которые взяли FOR SHARE до области; под областью их не блокируем"""
    from quoll.auth.audit import record
    from quoll.auth.audit_models import TargetType
    from quoll.interactions.branch_service import close_locked
    from quoll.interactions.close_reasons import id_by_code

    interaction = scope.interaction
    actions = sorted(
        await actions_of(session, sa.id),
        key=lambda a: (_ORDER[ActionType(a.type)], a.id),
    )
    label = sa_lifecycle.label(sa)
    moved = "Agreement targets changed, approve again"
    start = None
    if any(a.type in (ActionType.NEW_BRANCH, ActionType.RESUME) for a in actions):
        start = await branch_start(session, interaction.workflow_id)
    for action in actions:
        if action.type == ActionType.NEW_BRANCH:
            if start is None or start.id not in shared:
                raise DomainRuleException(409, moved)
            branch = await session.scalar(
                select(Branch).where(
                    Branch.interaction_id == interaction.id,
                    Branch.program_id.is_not_distinct_from(action.program_id),
                    Branch.product_id.is_not_distinct_from(action.product_id),
                    Branch.closed_at.is_(None),
                    Branch.state_id.is_(None),
                )
            )
            if branch is None:
                latest = await session.scalar(
                    select(func.max(Branch.iteration)).where(
                        Branch.interaction_id == interaction.id,
                        Branch.program_id.is_not_distinct_from(action.program_id),
                        Branch.product_id.is_not_distinct_from(action.product_id),
                    )
                )
                branch = Branch(
                    interaction_id=interaction.id,
                    program_id=action.program_id,
                    product_id=action.product_id,
                    added_by=actor_id,
                    iteration=(latest or 0) + 1,
                )
                session.add(branch)
            branch.contract_status = "APPROVED"
            branch.origin = "SUPPLEMENTARY_AGREEMENT"
            branch.supplementary_agreement_id = sa.id
            branch.state_id = start.id
            branch.opened_at = func.now()
            branch.stall_since = func.now()
            await session.flush()
            action.result_branch_id = branch.id
            sa_lifecycle.history(
                session,
                interaction,
                StageChangeKind.BRANCH_ADDED,
                actor_id,
                branch_id=branch.id,
                to_stage_id=start.id,
                comment=label,
                payload={"sa_id": sa.id},
            )
            continue
        branch = await session.get(Branch, action.branch_id, populate_existing=True)
        if action.type == ActionType.RESUME:
            stage = await session.get(Stage, branch.state_id, populate_existing=True)
            if stage.id not in shared or (
                stage.archived_at is not None and not stage.is_terminal
            ):
                raise DomainRuleException(409, moved)
            if stage.is_terminal:
                if start is None or start.id not in shared:
                    raise DomainRuleException(409, moved)
                license = {
                    "signed_at": _plain(branch.license_signed_at),
                    "term_years": branch.license_term_years,
                    "until": _plain(branch.license_until),
                }
                branch.state_id = start.id
                sa_lifecycle.history(
                    session,
                    interaction,
                    StageChangeKind.RESTART,
                    actor_id,
                    branch_id=branch.id,
                    from_stage_id=stage.id,
                    to_stage_id=start.id,
                    comment=label,
                    payload={"sa_id": sa.id, "license": license},
                )
            else:
                sa_lifecycle.history(
                    session,
                    interaction,
                    StageChangeKind.REOPEN,
                    actor_id,
                    branch_id=branch.id,
                    from_stage_id=stage.id,
                    to_stage_id=stage.id,
                    comment=label,
                    payload={"sa_id": sa.id},
                )
            branch.closed_at = None
            branch.close_reason_id = None
            branch.closed_with_interaction = False
            branch.stall_since = func.now()
            await session.flush()
        elif action.type == ActionType.EXTEND_LICENSE:
            old = branch.license_until
            branch.license_until = action.license_until
            payload = {
                "sa_id": sa.id,
                "old": _plain(old),
                "new": _plain(action.license_until),
            }
            sa_lifecycle.history(
                session,
                interaction,
                StageChangeKind.LICENSE_EXTENDED,
                actor_id,
                branch_id=branch.id,
                from_stage_id=branch.state_id,
                to_stage_id=branch.state_id,
                comment=f"{payload['old']} → {payload['new']}, {label}",
                payload=payload,
            )
            record(
                session,
                actor_id=actor_id,
                event_type=AuditEventType.LICENSE_EXTENDED,
                target_type=TargetType.INTERACTION,
                target_id=interaction.id,
                old_value={"branch_id": branch.id, "license_until": payload["old"]},
                new_value={"license_until": payload["new"], "sa_id": sa.id},
            )
        elif action.type == ActionType.EXTEND_CONTRACT:
            contract = await current_contract(session, interaction.id)
            old = contract.contract_valid_until
            contract.contract_valid_until = action.contract_valid_until
            payload = {
                "sa_id": sa.id,
                "old": _plain(old),
                "new": _plain(action.contract_valid_until),
                "document_id": contract.id,
            }
            sa_lifecycle.history(
                session,
                interaction,
                StageChangeKind.CONTRACT_EXTENDED,
                actor_id,
                comment=f"{payload['old']} → {payload['new']}, {label}",
                payload=payload,
            )
            record(
                session,
                actor_id=actor_id,
                event_type=AuditEventType.CONTRACT_EXTENDED,
                target_type=TargetType.INTERACTION,
                target_id=interaction.id,
                old_value={"contract_valid_until": payload["old"]},
                new_value={"contract_valid_until": payload["new"], "sa_id": sa.id},
            )
        elif action.type == ActionType.EXCLUDE:
            await close_locked(
                session,
                scope,
                branch,
                close_reason_id=await id_by_code(session, "EXCLUDED_BY_AGREEMENT"),
                comment=label,
                allow_system_reason=True,
            )
    sa.status = AgreementStatus.APPROVED
    sa.decided_by = actor_id
    sa.decided_at = func.now()
    sa.decision_comment = comment
    await sa_lifecycle.pass_history(
        session,
        interaction,
        StageChangeKind.SA_APPROVED,
        actor_id,
        sa,
        payload={"sa_id": sa.id, "actions": [a.id for a in actions]},
    )
    sa_lifecycle.journal(session, actor_id, AuditEventType.SA_APPROVED, sa)
    await session.flush()
