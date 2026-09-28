"""Правка справочников: одна схема на все - запись, журнал, отказ по ссылкам"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import ColumnElement, insert, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.catalog.models import DocumentKind
from quoll.core.base_repository import BaseRepository
from quoll.core.exceptions import DomainRuleException, IdNotExistsException
from quoll.db import Base
from quoll.workflows.models import WorkflowTransition


# поле «*_ids» схемы -> связь модели и модель её элементов
def _links() -> dict[type, dict[str, tuple[str, type]]]:
    from quoll.catalog.models import ItDirection, ItProgram, Product, Specialty

    return {
        Product: {"direction_ids": ("directions", ItDirection)},
        ItProgram: {"product_ids": ("products", Product)},
        Specialty: {"direction_ids": ("directions", ItDirection)},
    }


async def _apply_links(
    session: AsyncSession, item: Base, links: dict[str, list[int]], *, new: bool = False
) -> dict[str, list[int]]:
    """связи многие ко многим; неизвестный id - 400. Вернёт прежние id"""
    old = {}
    for field, ids in links.items():
        attr, target = _links()[type(item)][field]
        found = list(
            await session.scalars(select(target).where(target.id.in_(set(ids))))
        )
        missing = set(ids) - {f.id for f in found}
        if missing:
            raise DomainRuleException(
                400, f"{target.__name__} {sorted(missing)} does not exist"
            )
        # у нового объекта прежних связей нет, а чтение запустило бы загрузку
        if not new:
            old[field] = [x.id for x in getattr(item, attr)]
        setattr(item, attr, found)
    return old


def _journal_value(target: TargetType, values: dict[str, Any]) -> dict[str, Any]:
    """у контактов в журнал - только какие поля менялись, без ПДн (М 9)"""
    if target == TargetType.CONTACT:
        return {"fields": sorted(values)}
    return values


def _split(model: type[Base], data: dict[str, Any]) -> tuple[dict, dict]:
    known = _links().get(model, {})
    links = {k: v for k, v in data.items() if k in known}
    return {k: v for k, v in data.items() if k not in known}, links


async def create(
    session: AsyncSession,
    model: type[Base],
    target: TargetType,
    data: BaseModel,
    actor_id: str | None,
) -> Base:
    fields, links = _split(model, data.model_dump())
    item = model(**fields)
    await _apply_links(session, item, links, new=True)
    session.add(item)
    await session.flush()
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.CATALOG_ITEM_CREATED,
        target_type=target,
        target_id=item.id,
        new_value=_journal_value(target, data.model_dump(mode="json")),
    )
    await session.refresh(item)
    return item


async def update(
    session: AsyncSession,
    model: type[Base],
    target: TargetType,
    item_id: int,
    changes: BaseModel | dict[str, Any],
    actor_id: str | None,
) -> Base:
    new = (
        changes if isinstance(changes, dict) else changes.model_dump(exclude_unset=True)
    )
    item = await BaseRepository(session, model).get(item_id)
    fields, links = _split(model, new)
    old = {k: getattr(item, k) for k in fields}
    for k, v in fields.items():
        setattr(item, k, v)
    old |= await _apply_links(session, item, links)
    await session.flush()
    if new:
        record(
            session,
            actor_id=actor_id,
            event_type=AuditEventType.CATALOG_ITEM_UPDATED,
            target_type=target,
            target_id=item_id,
            old_value=_journal_value(target, old),
            new_value=_journal_value(target, new),
        )
    await session.refresh(item)
    return item


async def delete(
    session: AsyncSession,
    model: type[Base],
    target: TargetType,
    item_id: int,
    actor_id: str,
) -> None:
    """на что-то ссылается - 409 от обработчика IntegrityError, не 500"""
    item = await session.get(model, item_id)
    if getattr(item, "is_system", False):
        raise DomainRuleException(409, "Built-in entry is not deleted")
    if model is DocumentKind:
        await _check_kind_removable(session, item_id)
    if not await BaseRepository(session, model).delete(item_id):
        raise IdNotExistsException(model.__name__)
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.CATALOG_ITEM_DELETED,
        target_type=target,
        target_id=item_id,
    )


async def _check_kind_removable(session: AsyncSession, kind_id: int) -> None:
    """документы держит внешний ключ, а рёбра хранят коды списком - их
    проверяем сами"""
    kind = await session.get(DocumentKind, kind_id)
    if kind is None:
        return
    if await session.scalar(
        select(WorkflowTransition.id)
        .where(WorkflowTransition.required_document_kinds.contains([kind.code]))
        .limit(1)
    ):
        raise DomainRuleException(
            409, f"Document kind '{kind.code}' is required by a transition"
        )


async def listing(
    session: AsyncSession,
    model: type[Base],
    filters: list[ColumnElement[bool]],
    order: list[Any],
    limit: int,
    offset: int,
) -> list[Base]:
    """фильтр до limit - иначе страница врала бы"""
    stmt = select(model).where(*filters).order_by(*order).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


async def set_university_specialties(
    session: AsyncSession, university_id: int, specialty_ids: list[int], actor_id: str
) -> list:
    from quoll.catalog.models import Specialty, university_specialties
    from quoll.interactions.models import University

    if await session.get(University, university_id) is None:
        raise IdNotExistsException(University.__name__)
    found = list(
        await session.scalars(
            select(Specialty).where(Specialty.id.in_(set(specialty_ids)))
        )
    )
    if len(found) != len(set(specialty_ids)):
        raise DomainRuleException(400, "Some specialties do not exist")
    link = university_specialties
    old = list(
        await session.scalars(
            select(link.c.specialty_id).where(link.c.university_id == university_id)
        )
    )
    await session.execute(sa_delete(link).where(link.c.university_id == university_id))
    if found:
        await session.execute(
            insert(link),
            [{"university_id": university_id, "specialty_id": s.id} for s in found],
        )
    record(
        session,
        actor_id=actor_id,
        event_type=AuditEventType.CATALOG_ITEM_UPDATED,
        target_type=TargetType.UNIVERSITY,
        target_id=university_id,
        old_value={"specialty_ids": sorted(old)},
        new_value={"specialty_ids": sorted(s.id for s in found)},
    )
    return sorted(found, key=lambda s: s.code)


async def check_contact_owner(
    session: AsyncSession, user, university_id: int | None, vendor_id: int | None
) -> None:
    """контакты вуза правят КАМ своих вузов, его руководитель и админ,
    вендора - только админ. Свой вуз - где есть незакрытая заявка КАМа (Р1)"""
    from quoll.auth.models import Manager, UserRole
    from quoll.core.exceptions import OperationForbiddenException
    from quoll.interactions.models import Interaction

    if user.role == UserRole.ADMIN:
        return
    if university_id is not None and user.role in (
        UserRole.MANAGER,
        UserRole.SUPERVISER,
    ):
        # КАМ - своих вузов; руководитель - вузов своих КАМов (как у заявки)
        owner = (
            Interaction.owner_id == user.id
            if user.role == UserRole.MANAGER
            else Interaction.owner_id.in_(
                select(Manager.id).where(Manager.superviser_id == user.id)
            )
        )
        own = await session.scalar(
            select(Interaction.id)
            .where(
                Interaction.university_id == university_id,
                owner,
                Interaction.closed_at.is_(None),
            )
            .limit(1)
        )
        if own is not None:
            return
    raise OperationForbiddenException("change contacts of this organisation")


async def suggest_programs(session: AsyncSession, university_id: int) -> list[dict]:
    """активные программы по числу специальностей вуза, связанных с их
    направлением; при равенстве - по приоритету и названию (М 3.21)"""
    from quoll.catalog.models import (
        ItProgram,
        Specialty,
        specialty_directions,
        university_specialties,
    )

    rows = await session.execute(
        select(specialty_directions.c.direction_id, Specialty.code, Specialty.name)
        .join(Specialty, Specialty.id == specialty_directions.c.specialty_id)
        .join(
            university_specialties,
            university_specialties.c.specialty_id == Specialty.id,
        )
        .where(university_specialties.c.university_id == university_id)
        .order_by(Specialty.code)
    )
    by_direction: dict[int, list[dict]] = {}
    for direction_id, code, name in rows:
        by_direction.setdefault(direction_id, []).append({"code": code, "name": name})
    programs = await session.scalars(
        select(ItProgram).where(ItProgram.is_active.is_(True))
    )
    suggestions = [
        {"program": p, "matched": by_direction.get(p.direction_id, [])}
        for p in programs
    ]
    suggestions.sort(
        key=lambda s: (
            -len(s["matched"]),
            s["program"].priority is None,
            s["program"].priority or 0,
            s["program"].name,
        )
    )
    return suggestions
