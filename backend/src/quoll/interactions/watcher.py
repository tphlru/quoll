"""Сторож: застой, сроки лицензий и договоров (О 5, 15, 16; Т-3).

кандидатов выбираем без блокировок, дальше - своя транзакция на каждую
заявку под системным захватом области: иначе блокировки копились бы весь
проход и встречались бы с живыми операциями. Условие перепроверяется под
блокировкой. Повтор не страшен: у каждого повода свой ключ, и второе
уведомление не создаётся (О 15: одно на отсчёт застоя)
"""

import logging
from datetime import date, datetime

from sqlalchemy import Integer, String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from quoll.interactions.bindings import BUSINESS_TZ, current_contract
from quoll.interactions.models import (
    Branch,
    Interaction,
    PauseState,
    SlotKind,
)
from quoll.interactions.notify import notify
from quoll.interactions.scope import lock_interaction_scope
from quoll.notifications import kinds
from quoll.notifications.kinds import Subject
from quoll.workflows.models import Stage, Workflow

logger = logging.getLogger(__name__)


def _threshold(stage):
    """порог руководителя на этот шаг, иначе - шага; пусто - застоя нет"""
    override = Interaction.stall_overrides.op("->>")(cast(stage.id, String))
    return func.coalesce(cast(override, Integer), stage.stall_days)


def _stalled(since, threshold):
    return and_(
        since.is_not(None),
        threshold.is_not(None),
        since + func.make_interval(0, 0, 0, threshold) <= func.now(),
    )


def _interaction_stall():
    """до подписания: после него застой считают ветки (П8)"""
    threshold = _threshold(Stage)
    return (
        select(Interaction.id, Interaction.stall_since, Stage.name, threshold)
        .join(Stage, Stage.id == Interaction.state_id)
        .where(
            Interaction.closed_at.is_(None),
            Interaction.is_paused.is_(False),
            Interaction.no_return_at.is_(None),
            _stalled(Interaction.stall_since, threshold),
        )
    )


def _branch_stall():
    """открытые ветки не на паузе - ни своей, ни заявки"""
    stage = aliased(Stage)
    threshold = _threshold(stage)
    return (
        select(
            Branch.interaction_id, Branch.id, Branch.stall_since, stage.name, threshold
        )
        .join(stage, stage.id == Branch.state_id)
        .join(Interaction, Interaction.id == Branch.interaction_id)
        .where(
            Branch.closed_at.is_(None),
            Branch.pause_state == PauseState.ACTIVE,
            Interaction.closed_at.is_(None),
            Interaction.is_paused.is_(False),
            _stalled(Branch.stall_since, threshold),
        )
    )


def warning_days(days_left: int, warn: list[int]) -> int | None:
    """за сколько дней предупредить сейчас: наименьший подходящий срок, чтобы
    ветка, заведённая за 20 дней до конца, получила одно, а не два"""
    matching = [n for n in warn if 0 <= days_left <= n]
    return min(matching) if matching else None


async def _each(session_maker, interaction_ids, handle) -> int:
    sent = 0
    for interaction_id in sorted(set(interaction_ids)):
        try:
            async with session_maker() as db:
                scope = await lock_interaction_scope(db, interaction_id, None)
                sent += await handle(db, scope)
                await db.commit()
        except Exception:
            # одна сломанная заявка не останавливает проход
            logger.exception(f"Watcher failed on interaction {interaction_id}")
    return sent


async def _stall_tick(session_maker: async_sessionmaker) -> int:
    async with session_maker() as db:
        interactions = (await db.execute(_interaction_stall())).all()
        branches = (await db.execute(_branch_stall())).all()
    candidates = [row[0] for row in (*interactions, *branches)]

    async def handle(db: AsyncSession, scope) -> int:
        sent = 0
        iid = scope.interaction.id
        for _, since, stage, days in await db.execute(
            _interaction_stall().where(Interaction.id == iid)
        ):
            await notify(
                db,
                kinds.STALL,
                scope,
                context={"branch": "", "stage": stage, "days": days},
                dedup_key=f"stall:interaction:{iid}:{since.isoformat()}",
            )
            sent += 1
        for _, branch_id, since, stage, days in await db.execute(
            _branch_stall().where(Branch.interaction_id == iid)
        ):
            await notify(
                db,
                kinds.STALL,
                scope,
                context={
                    "branch": f", ветка {branch_id}",
                    "stage": stage,
                    "days": days,
                },
                subject=Subject.BRANCH,
                subject_id=branch_id,
                payload={"branch_id": branch_id},
                dedup_key=f"stall:branch:{branch_id}:{since.isoformat()}",
            )
            sent += 1
        return sent

    return await _each(session_maker, candidates, handle)


async def _warn_list(db: AsyncSession, interaction: Interaction) -> list[int]:
    if interaction.warn_days is not None:
        return interaction.warn_days
    workflow = await db.get(Workflow, interaction.workflow_id)
    return workflow.warn_days if workflow else []


async def _expiry_tick(session_maker: async_sessionmaker) -> int:
    today = datetime.now(BUSINESS_TZ).date()
    async with session_maker() as db:
        candidates = list(
            await db.scalars(
                select(Interaction.id).where(
                    Interaction.closed_at.is_(None),
                    Interaction.no_return_at.is_not(None),
                )
            )
        )

    async def handle(db: AsyncSession, scope) -> int:
        interaction = scope.interaction
        if interaction.closed_at is not None:
            return 0
        warn = await _warn_list(db, interaction)
        sent = 0
        branches = await db.scalars(
            select(Branch).where(
                Branch.interaction_id == interaction.id,
                Branch.state_id.is_not(None),
                Branch.closed_at.is_(None),
                Branch.license_until.is_not(None),
            )
        )
        for branch in branches:
            sent += await _warn(
                db,
                scope,
                kinds.LICENSE_EXPIRING,
                branch.license_until,
                today,
                warn,
                key=f"license:{branch.id}",
                branch_id=branch.id,
            )
        contract = await current_contract(db, interaction.id)
        if contract is not None and contract.contract_valid_until is not None:
            sent += await _warn(
                db,
                scope,
                kinds.CONTRACT_EXPIRING,
                contract.contract_valid_until,
                today,
                warn,
                key=f"contract:{interaction.id}",
            )
        return sent

    return await _each(session_maker, candidates, handle)


async def _warn(
    db, scope, kind, until: date, today: date, warn, *, key, branch_id=None
) -> int:
    days = warning_days((until - today).days, warn)
    if days is None:
        return 0
    await notify(
        db,
        kind,
        scope,
        context={
            "branch": f", ветка {branch_id}" if branch_id else "",
            "until": until.strftime("%d.%m.%Y"),
        },
        subject=Subject.BRANCH if branch_id else Subject.INTERACTION,
        subject_id=branch_id,
        payload={"branch_id": branch_id} if branch_id else None,
        # продление меняет дату - и ключ, предупреждение придёт снова
        dedup_key=f"{key}:{until.isoformat()}:{days}",
    )
    return 1


async def _passive_tick(session_maker: async_sessionmaker) -> int:
    """давно без действий на долгосрочных этапах - в пассивные (Д19).
    Обратно сторож не переводит никогда"""
    from quoll.interactions.project_service import set_slot
    from quoll.interactions.slots import passive_problem

    async with session_maker() as db:
        candidates = list(
            await db.scalars(
                select(Interaction.id).where(
                    Interaction.slot == SlotKind.ACTIVE,
                    Interaction.no_return_at.is_not(None),
                    Interaction.closed_at.is_(None),
                )
            )
        )

    async def handle(db: AsyncSession, scope) -> int:
        interaction = scope.interaction
        if interaction.slot != SlotKind.ACTIVE:
            return 0
        if await passive_problem(db, interaction, idle=True) is not None:
            return 0
        await set_slot(db, interaction, SlotKind.PASSIVE, None)
        await notify(db, kinds.MOVED_TO_PASSIVE, scope)
        return 1

    return await _each(session_maker, candidates, handle)


async def watch(session_maker: async_sessionmaker) -> int:
    """один проход; возвращает, сколько поводов обработано"""
    return (
        await _stall_tick(session_maker)
        + await _expiry_tick(session_maker)
        + await _passive_tick(session_maker)
    )
