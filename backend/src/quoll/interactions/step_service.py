"""Значения полей шага: номер договора, срок лицензии, число обученных.

правят владелец и его руководитель - на текущем и на пройденных шагах: поле
«число обученных» дописывают со временем. Под захватом области: переход
проверяет обязательные поля под той же блокировкой
"""

from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.auth.models import UserRole
from quoll.core.exceptions import DomainRuleException, OperationForbiddenException
from quoll.interactions import contract_service, step_contacts
from quoll.interactions.access_policy import can_change, can_close
from quoll.interactions.bindings import extension_locks, read_bound, write_bound
from quoll.interactions.models import (
    Branch,
    InteractionStageHistory,
    InteractionStageValues,
)
from quoll.interactions.notify import notify
from quoll.interactions.scope import lock_interaction_scope
from quoll.interactions.step_policy import value_problems
from quoll.interactions.transition_service import stage_values
from quoll.notifications import kinds
from quoll.workflows.models import Stage


async def set_values(
    session: AsyncSession,
    *,
    interaction_id: int,
    stage_id: int,
    values: dict[str, Any],
    actor_id: str,
    branch_id: int | None = None,
    side_pointer_id: int | None = None,
) -> InteractionStageValues:
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    interaction = scope.interaction
    if not can_change(scope.actor, scope.ownership):
        raise OperationForbiddenException("fill steps of this interaction")
    stage = await session.get(Stage, stage_id)
    if stage is None or stage.workflow_id != interaction.workflow_id:
        raise DomainRuleException(400, "Stage belongs to another workflow")
    # у шага ветки - значения своей ветки, у шага договора - общие, у доп.
    # прохождения - свои
    current = interaction.state_id
    if side_pointer_id is not None:
        pointer = await contract_service.active_pass(
            session, interaction_id, side_pointer_id
        )
        contract_service.check_side_target(stage, branch_id)
        current = pointer.stage_id
    if branch_id is not None:
        branch = await session.get(Branch, branch_id)
        if branch is None or branch.interaction_id != interaction_id:
            raise DomainRuleException(404, "Branch is not in this interaction")
        if branch.state_id is None:
            raise DomainRuleException(
                409, "Branch is a draft until the contract is signed"
            )
        current = branch.state_id
    if stage.is_branch_stage != (branch_id is not None):
        raise DomainRuleException(400, "Branch stages are filled per branch")
    visited = await session.scalar(
        select(
            exists().where(
                InteractionStageHistory.interaction_id == interaction_id,
                InteractionStageHistory.branch_id.is_not_distinct_from(branch_id),
                InteractionStageHistory.side_pointer_id.is_not_distinct_from(
                    side_pointer_id
                ),
                # пройдена - и та, куда пришли, и та, с которой ушли
                or_(
                    InteractionStageHistory.to_stage_id == stage_id,
                    InteractionStageHistory.from_stage_id == stage_id,
                ),
            )
        )
    )
    if stage_id != current and not visited:
        raise DomainRuleException(409, "Stage is not reached yet")
    if problems := value_problems(stage.fields, values):
        raise DomainRuleException(422, "; ".join(problems))
    # контакты - сразу в справочник, в шаге остаётся ссылка
    values = await step_contacts.resolve(
        session, stage, interaction.university_id, values, actor_id
    )

    old = await stage_values(
        session,
        interaction_id,
        stage_id,
        branch_id,
        side_pointer_id=side_pointer_id,
        expand=False,
    )
    gated = {f["key"] for f in stage.fields if f.get("approval_after_pass")}
    changed = {k for k in old.keys() | values.keys() if old.get(k) != values.get(k)}
    if side_pointer_id is not None:
        passed = stage_id != current
    else:
        passed = contract_service.step_passed(interaction, stage_id, current, branch_id)
    if scope.actor.role == UserRole.MANAGER and passed and changed & gated:
        # правку, которую всё равно не одобрить (продлено допсоглашением), не заводим
        await extension_locks(
            session,
            stage,
            interaction_id,
            branch_id,
            {k: values.get(k) for k in changed & gated},
        )
        # правку пройденного шага с такими полями одобряет руководитель
        row = await _upsert(
            session, interaction_id, stage_id, branch_id, side_pointer_id, old, actor_id
        )
        # ждут только поля с аппрувом; остальное применяется сразу
        free = {k: v for k, v in values.items() if k not in gated}
        kept = {k: v for k, v in old.items() if k in gated}
        if free != {k: v for k, v in old.items() if k not in gated}:
            _journal(session, actor_id, interaction_id, stage_id, old, kept | free)
            row.values = kept | free
            await write_bound(session, stage, interaction_id, branch_id, free)
        row.pending_values = {k: values.get(k) for k in changed & gated}
        row.pending_by = actor_id
        await session.flush()
        # updated_at ставит база - без refresh ответ полез бы за ним вне greenlet
        await session.refresh(row)
        await notify(
            session,
            kinds.STEP_EDIT_PENDING,
            scope,
            context={"stage": stage.name, "fields": ", ".join(sorted(changed & gated))},
            payload={"stage_id": stage_id, "branch_id": branch_id},
        )
        return row

    await write_bound(session, stage, interaction_id, branch_id, values)
    row = await _upsert(
        session, interaction_id, stage_id, branch_id, side_pointer_id, values, actor_id
    )
    _journal(session, actor_id, interaction_id, stage_id, old, values)
    return row


async def decide(
    session: AsyncSession,
    *,
    interaction_id: int,
    stage_id: int,
    actor_id: str,
    approve: bool,
    branch_id: int | None = None,
    side_pointer_id: int | None = None,
) -> InteractionStageValues:
    """руководитель владельца решает по ждущей правке пройденного шага"""
    scope = await lock_interaction_scope(session, interaction_id, actor_id)
    if not can_close(scope.actor, scope.ownership):
        raise OperationForbiddenException("decide on step values")
    if side_pointer_id is not None:
        await contract_service.active_pass(session, interaction_id, side_pointer_id)
    row = await session.scalar(
        select(InteractionStageValues)
        .where(
            InteractionStageValues.interaction_id == interaction_id,
            InteractionStageValues.stage_id == stage_id,
            InteractionStageValues.branch_id.is_not_distinct_from(branch_id),
            InteractionStageValues.side_pointer_id.is_not_distinct_from(
                side_pointer_id
            ),
        )
        .execution_options(populate_existing=True)
    )
    if row is None or row.pending_values is None:
        raise DomainRuleException(409, "No step values wait for a decision")
    author, pending = row.pending_by, row.pending_values
    row.pending_values = None
    row.pending_by = None
    if approve:
        # вливаем только одобренные поля - правки после подачи не затираются
        merged = {**row.values, **pending}
        _journal(session, actor_id, interaction_id, stage_id, row.values, merged)
        stage = await session.get(Stage, stage_id)
        await write_bound(session, stage, interaction_id, branch_id, pending)
        row.values = merged
        row.updated_by = author
    await session.flush()
    stage = await session.get(Stage, stage_id)
    await notify(
        session,
        kinds.STEP_EDIT_DECIDED,
        scope,
        context={
            "decision": "одобрена" if approve else "отклонена",
            "stage": stage.name,
        },
        payload={"stage_id": stage_id, "branch_id": branch_id},
        editor_id=author,
    )
    await session.refresh(row)
    return row


async def _upsert(
    session: AsyncSession,
    interaction_id: int,
    stage_id: int,
    branch_id: int | None,
    side_pointer_id: int | None,
    values: dict[str, Any],
    actor_id: str,
) -> InteractionStageValues:
    table = InteractionStageValues.__table__
    row_id = await session.scalar(
        pg_insert(table)
        .values(
            interaction_id=interaction_id,
            stage_id=stage_id,
            branch_id=branch_id,
            side_pointer_id=side_pointer_id,
            values=values,
            updated_by=actor_id,
        )
        .on_conflict_do_update(
            index_elements=[
                "interaction_id",
                "stage_id",
                "branch_id",
                "side_pointer_id",
            ],
            set_={"values": values, "updated_by": actor_id, "updated_at": func.now()},
        )
        .returning(table.c.id)
    )
    return await session.get(InteractionStageValues, row_id, populate_existing=True)


def _journal(session, actor_id, interaction_id, stage_id, old, new) -> None:
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.STAGE_VALUES_CHANGED,
        target_type=TargetType.INTERACTION,
        target_id=interaction_id,
        old_value={"stage_id": stage_id, "values": old},
        new_value={"stage_id": stage_id, "values": new},
    )


async def all_values(session: AsyncSession, interaction_id: int) -> list[dict]:
    rows = await session.scalars(
        select(InteractionStageValues)
        .where(InteractionStageValues.interaction_id == interaction_id)
        .order_by(InteractionStageValues.stage_id)
    )
    return [await view(session, row) for row in rows]


async def view(session: AsyncSession, row: InteractionStageValues) -> dict:
    """привязанные поля - из колонок: их могли поменять не через шаг
    (реквизиты при загрузке договора, продление допсоглашением)"""
    stage = await session.get(Stage, row.stage_id)
    bound = await read_bound(session, stage, row.interaction_id, row.branch_id)
    values = await step_contacts.expand(
        session, stage, {**row.values, **bound}, for_view=True
    )
    return {
        "stage_id": row.stage_id,
        "branch_id": row.branch_id,
        "side_pointer_id": row.side_pointer_id,
        "values": values,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
        "pending_values": row.pending_values,
        "pending_by": row.pending_by,
    }
