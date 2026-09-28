"""Хуки обработчиков доп. шагов - один диспетчер, для стадий без handler
всё no-op (§6.5). Поведение ДС живёт в sa_lifecycle/sa_service, импорт
внутри функций - иначе цикл с transition_service"""

from sqlalchemy.ext.asyncio import AsyncSession

from quoll.core.exceptions import DomainRuleException
from quoll.interactions.models import StageChangeKind
from quoll.interactions.scope import InteractionScope
from quoll.workflows.models import Stage, WorkflowTransition
from quoll.workflows.step_handlers import SUPPLEMENTARY_AGREEMENT


async def enter(
    session: AsyncSession,
    scope: InteractionScope,
    stage: Stage,
    pass_id: int | None,
    actor_id: str | None,
) -> None:
    """прохождение встало на стадию: переход, возврат при отказе, откат, start"""
    if stage.handler == SUPPLEMENTARY_AGREEMENT:
        from quoll.interactions import sa_lifecycle

        await sa_lifecycle.open_for_pass(session, scope.interaction, pass_id, actor_id)


async def check_leave(
    session: AsyncSession,
    scope: InteractionScope,
    stage: Stage,
    edge: WorkflowTransition,
    pass_id: int | None,
) -> None:
    """перед прямым выходом с шага: прямой ход и создание просьбы TRANSITION"""
    if stage.handler == SUPPLEMENTARY_AGREEMENT:
        from quoll.interactions import sa_service

        sa = await sa_service.open_of_pass(session, scope.interaction.id, pass_id)
        if sa is None:
            raise DomainRuleException(409, "Agreement is not ready: it is not open")
        if problems := await sa_service.problems(session, scope.interaction, sa):
            raise DomainRuleException(
                409, "Agreement is not ready: " + "; ".join(problems)
            )


async def leave(
    session: AsyncSession,
    scope: InteractionScope,
    stage: Stage,
    edge: WorkflowTransition,
    pass_id: int | None,
    shared: set[int] | frozenset[int],
    actor_id: str | None,
    comment: str | None,
) -> None:
    """прямой выход состоялся (после check_step): ДС применяется"""
    if stage.handler == SUPPLEMENTARY_AGREEMENT:
        from quoll.interactions import sa_service

        await check_leave(session, scope, stage, edge, pass_id)
        sa = await sa_service.open_of_pass(session, scope.interaction.id, pass_id)
        await sa_service.apply(session, scope, sa, actor_id, comment, shared)


async def abandon(
    session: AsyncSession,
    scope: InteractionScope,
    stage: Stage,
    pass_id: int | None,
    reason: str,
    actor_id: str | None,
) -> None:
    """прохождение ушло с шага без выхода вперёд: обратное ребро, возврат,
    откат, отмена доп. указателя"""
    if stage.handler == SUPPLEMENTARY_AGREEMENT:
        from quoll.interactions import sa_lifecycle

        await sa_lifecycle.cancel_open(
            session, scope.interaction, actor_id, reason, pass_id=pass_id
        )


async def pre_share(
    session: AsyncSession, interaction_id: int, pass_id: int | None, to_stage_id: int
) -> set[int]:
    """стадии для FOR SHARE до области, без блокировок: незавершённое ДС
    прохождения есть только на его шаге (I5), нужны лишь при прямом выходе"""
    from quoll.interactions import sa_service

    sa = await sa_service.open_of_pass(session, interaction_id, pass_id)
    if sa is None:
        return set()
    stage = await sa_service.stage_of(session, sa)
    if stage is None or not await sa_service.is_forward_exit(
        session, stage, to_stage_id
    ):
        return set()
    return await sa_service.stages_to_share(session, sa)


async def submitted(
    session: AsyncSession,
    scope: InteractionScope,
    stage: Stage,
    pass_id: int | None,
    actor_id: str,
) -> None:
    """создана просьба TRANSITION с этого шага"""
    if stage.handler == SUPPLEMENTARY_AGREEMENT:
        from quoll.interactions import sa_service

        sa = await sa_service.open_of_pass(session, scope.interaction.id, pass_id)
        sa_service.submit(session, sa, actor_id)


async def returned(
    session: AsyncSession,
    scope: InteractionScope,
    pass_id: int | None,
    reason: str,
    kind: StageChangeKind,
    actor_id: str | None,
) -> None:
    """просьба с шага обработчика отклонена, отозвана или отменена - зовут
    только для таких. kind - SA_REJECTED при отказе, иначе SA_RETURNED"""
    from quoll.interactions import sa_lifecycle, sa_service

    sa = await sa_service.open_of_pass(session, scope.interaction.id, pass_id)
    if sa is not None:
        await sa_lifecycle.return_to_draft(
            session, scope.interaction, sa, reason, actor_id, kind
        )
