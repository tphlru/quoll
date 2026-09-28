"""Применение строки каталога (import-design §18).

Записи создаются и меняются функциями каталога; автор - система (Т4).
Ссылки на записи партии к этому моменту - id: предыдущие виды уже в базе,
а свои части и слитые повторы берутся из итогов строк-источников
"""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.catalog import service as catalog_service
from quoll.catalog.models import university_specialties
from quoll.imports.analysis import MERGED, NEW, PARTS, UPDATE, RowCtx
from quoll.imports.errors import import_error
from quoll.imports.models import ImportRow


class _Refs:
    """ссылки части -> id"""

    def __init__(self, session: AsyncSession, row: ImportRow):
        self.session = session
        self.row = row
        self.own: dict[str, int] = {}

    async def id(self, value) -> Any:
        if not isinstance(value, dict):
            return value
        if "id" in value:
            return value["id"]
        row_id, part = value["new_of_row"], value.get("part")
        if row_id == self.row.id and part in self.own:
            return self.own[part]
        source = await self.session.get(ImportRow, row_id)
        found = (source.result_ids or {}).get(part) if source is not None else None
        if found is None:
            number = source.number if source is not None else None
            raise import_error(
                409, "IMP-142", "Source row was not applied", ref_row=number
            )
        return found

    async def value(self, value) -> Any:
        if isinstance(value, list):
            return [await self.id(v) for v in value]
        return await self.id(value)


async def apply_row(session: AsyncSession, ctx: RowCtx) -> dict[str, Any]:
    """части строки по порядку; вернёт id по частям (result_ids)"""
    refs = _Refs(session, ctx.row)
    out: dict[str, Any] = {}
    for part in ctx.parts:
        if part.name == "university_specialty":
            university, specialty = [await refs.id(v) for v in part.values["pair"]]
            if part.action == NEW:
                await _link_specialty(session, university, specialty)
            out[part.name] = [university, specialty]
            continue
        defn = PARTS[part.name]
        if part.action == NEW:
            data = {
                a: await refs.value(v)
                for a, v in part.values.items()
                if v is not None and v != []
            }
            item = await catalog_service.create(
                session, defn.model, defn.target, defn.write.model_validate(data), None
            )
            item_id = item.id
        elif part.action == MERGED:
            item_id = await refs.id(
                {"new_of_row": part.merged_into.row.id, "part": part.name}
            )
        else:
            item_id = part.item.id
            if part.action == UPDATE:
                changes = {a: await refs.value(v) for a, v in part.changes.items()}
                patch = defn.patch.model_validate(changes)
                await catalog_service.update(
                    session, defn.model, defn.target, item_id, patch, None
                )
        refs.own[part.name] = item_id
        out[part.name] = item_id
    return out


async def _link_specialty(
    session: AsyncSession, university_id: int, specialty_id: int
) -> None:
    """пара вуз - специальность только добавляется (§14.2)"""
    link = university_specialties
    added = await session.scalar(
        insert(link)
        .values(university_id=university_id, specialty_id=specialty_id)
        .on_conflict_do_nothing()
        .returning(link.c.specialty_id)
    )
    if added is None:
        return
    record(
        session,
        actor_id=None,
        event_type=AuditEventType.CATALOG_ITEM_UPDATED,
        target_type=TargetType.UNIVERSITY,
        target_id=university_id,
        new_value={"specialties_added": [specialty_id]},
    )
