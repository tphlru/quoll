"""Сколько заявок тянет менеджер."""

import logging
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.models import (
    IdentitySyncStatus,
    Manager,
    ManualWorkloadStatus,
    RoleTransitionStatus,
)
from quoll.core.exceptions import (
    CapacityExceededException,
    ManagerNotActiveException,
    ManagerUnavailableException,
)
from quoll.interactions.models import Interaction, SlotKind
from quoll.workflows.models import Stage

logger = logging.getLogger(__name__)


def counts_toward_capacity(stage: Stage | None, is_paused: bool, slot: str) -> bool:
    """занимает ли заявка активный слот менеджера.

    черновик без стадии, закрытая, на паузе и пассивная (Д19) - не занимают.
    доп. указатель слот не занимает - он не меняет stage заявки (Д42)
    """
    return (
        stage is not None
        and not stage.is_terminal
        and not is_paused
        and slot == SlotKind.ACTIVE
    )


def is_open_project(stage: Stage | None) -> bool:
    """открыта ли заявка. пауза не закрывает - т.е. всё ещё мешает
    открепить менеджера"""
    return stage is not None and not stage.is_terminal


# те же правила, но для SQL


def capacity_filter_expression() -> ColumnElement[bool]:
    return and_(
        Interaction.state_id.is_not(None),
        Interaction.closed_at.is_(None),
        Interaction.is_paused.is_(False),
        Interaction.slot == SlotKind.ACTIVE,
    )


def open_projects_filter_expression() -> ColumnElement[bool]:
    return and_(Interaction.state_id.is_not(None), Interaction.closed_at.is_(None))


def blocking_filter_expression() -> ColumnElement[bool]:
    """что мешает отпустить менеджера: незакрытые и черновики с ним,
    отменённые черновики - нет"""
    return Interaction.closed_at.is_(None)


def manager_load_subquery():
    """загрузка всех менеджеров одним запросом, включая тех, у кого заявок нет.

    условия - в FILTER при агрегате, а не в WHERE: в WHERE они выродили бы
    внешнее соединение во внутреннее, и менеджер без заявок пропал бы
    """
    managers = Manager.__table__
    counted = Interaction.id
    return (
        select(
            managers.c.id.label("manager_id"),
            func.count(counted)
            .filter(capacity_filter_expression())
            .label("capacity_used"),
            func.count(counted)
            .filter(open_projects_filter_expression())
            .label("open_projects"),
            func.count(counted)
            .filter(blocking_filter_expression())
            .label("blocking_projects"),
        )
        .select_from(managers)
        .outerjoin(Interaction, Interaction.owner_id == managers.c.id)
        .outerjoin(Stage, Interaction.state_id == Stage.id)
        .group_by(managers.c.id)
        .subquery("manager_load")
    )


class EffectiveStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    SATURATED = "SATURATED"
    UNAVAILABLE = "UNAVAILABLE"
    INACTIVE = "INACTIVE"


def effective_status_expression(
    capacity_used: ColumnElement[int],
) -> ColumnElement[str]:
    """готовность менеджера взять новую работу: первое сработавшее условие.
    AVAILABLE - то же, что пропускает assert_can_take_new_work с дельтой 1"""
    return case(
        (Manager.is_active.is_(False), EffectiveStatus.INACTIVE.value),
        (
            Manager.identity_sync_status != IdentitySyncStatus.OK,
            EffectiveStatus.UNAVAILABLE.value,
        ),
        (
            Manager.role_transition_status != RoleTransitionStatus.NONE,
            EffectiveStatus.UNAVAILABLE.value,
        ),
        (
            Manager.manual_workload_status != ManualWorkloadStatus.AVAILABLE,
            EffectiveStatus.UNAVAILABLE.value,
        ),
        (capacity_used >= Manager.max_active_projects, EffectiveStatus.SATURATED.value),
        else_=EffectiveStatus.AVAILABLE.value,
    )


def capacity_count_stmt(manager_id: str) -> Select[tuple[int]]:
    return (
        select(func.count())
        .select_from(Interaction)
        .join(Stage, Interaction.state_id == Stage.id)
        .where(Interaction.owner_id == manager_id, capacity_filter_expression())
    )


def _assert_can_work(manager: Manager) -> None:
    """в строю: активен, без конфликта ролей, не в смене роли"""
    if not manager.is_active:
        raise ManagerNotActiveException(manager.id)
    if manager.identity_sync_status != IdentitySyncStatus.OK:
        raise ManagerNotActiveException(manager.id, "role mapping conflict")
    if manager.role_transition_status != RoleTransitionStatus.NONE:
        raise ManagerNotActiveException(manager.id, "role transition in progress")


async def _assert_has_capacity(
    session: AsyncSession, manager: Manager, delta_slots: int
) -> None:
    if delta_slots <= 0:
        return
    current = await session.scalar(capacity_count_stmt(manager.id)) or 0
    if current + delta_slots > manager.max_active_projects:
        raise CapacityExceededException(
            manager.id, current, delta_slots, manager.max_active_projects
        )


async def assert_can_take_new_work(
    session: AsyncSession, manager: Manager, delta_slots: int
) -> None:
    """менеджеру дают работу: назначение, переоткрытие, передача.

    available проверяется при любой дельте - паузную заявку тоже не отдают
    тому, кто просил не давать новых. Строка менеджера уже должна быть
    заблокирована, иначе между подсчётом и записью влезет кто-то ещё
    """
    _assert_can_work(manager)
    if manager.manual_workload_status != ManualWorkloadStatus.AVAILABLE:
        raise ManagerUnavailableException(manager.id)
    await _assert_has_capacity(session, manager, delta_slots)


async def assert_can_keep_working(
    session: AsyncSession, manager: Manager, delta_slots: int
) -> None:
    """менеджер двигает свою заявку: переход, снятие с паузы.

    unavailable тут не мешает - это «не давайте новых», а не «не трогайте
    мои». Строка менеджера уже должна быть заблокирована
    """
    _assert_can_work(manager)
    await _assert_has_capacity(session, manager, delta_slots)
