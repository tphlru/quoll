"""Снимок базы для анализа партии (import-design §16.2).

Фиксированное число запросов на справочник; контакты (ПДн, SQL-ом не
ищутся) - лениво по владельцу. Объекты только читаются (M1)
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.catalog.models import (
    CloseReason,
    Contact,
    DocumentKind,
    ItDirection,
    ItProgram,
    Product,
    Specialty,
    university_specialties,
)
from quoll.imports.normalize import text_key
from quoll.interactions.models import University, Vendor


def _by(items, key) -> dict:
    out: dict = {}
    for item in items:
        out.setdefault(key(item), []).append(item)
    return out


@dataclass
class Snapshot:
    directions: dict[str, list[ItDirection]] = field(default_factory=dict)
    vendors: dict[str, list[Vendor]] = field(default_factory=dict)
    vendors_by_id: dict[int, Vendor] = field(default_factory=dict)
    # название -> продукты всех вендоров
    products: dict[str, list[Product]] = field(default_factory=dict)
    products_by_id: dict[int, Product] = field(default_factory=dict)
    programs: dict[str, list[ItProgram]] = field(default_factory=dict)
    programs_by_id: dict[int, ItProgram] = field(default_factory=dict)
    specialties: dict[str, Specialty] = field(default_factory=dict)
    universities: list[University] = field(default_factory=list)
    universities_by_id: dict[int, University] = field(default_factory=dict)
    university_specialties: set[tuple[int, int]] = field(default_factory=set)
    close_reasons: dict[str, CloseReason] = field(default_factory=dict)
    document_kinds: set[str] = field(default_factory=set)
    _contacts: dict[tuple[str, int], list[Contact]] = field(default_factory=dict)
    session: AsyncSession | None = None

    @classmethod
    async def load(cls, session: AsyncSession) -> "Snapshot":
        snap = cls(session=session)
        directions = list(await session.scalars(select(ItDirection)))
        snap.directions = _by(directions, lambda d: text_key(d.name))
        vendors = list(await session.scalars(select(Vendor)))
        snap.vendors = _by(vendors, lambda v: text_key(v.name))
        snap.vendors_by_id = {v.id: v for v in vendors}
        products = list(await session.scalars(select(Product)))
        snap.products = _by(products, lambda p: text_key(p.name))
        snap.products_by_id = {p.id: p for p in products}
        programs = list(await session.scalars(select(ItProgram)))
        snap.programs = _by(programs, lambda p: text_key(p.name))
        snap.programs_by_id = {p.id: p for p in programs}
        snap.specialties = {s.code: s for s in await session.scalars(select(Specialty))}
        snap.universities = list(await session.scalars(select(University)))
        snap.universities_by_id = {u.id: u for u in snap.universities}
        link = university_specialties
        snap.university_specialties = {
            (r.university_id, r.specialty_id)
            for r in await session.execute(
                select(link.c.university_id, link.c.specialty_id)
            )
        }
        snap.close_reasons = {
            r.code: r for r in await session.scalars(select(CloseReason))
        }
        snap.document_kinds = set(await session.scalars(select(DocumentKind.code)))
        return snap

    async def contacts(self, owner: str, owner_id: int) -> list[Contact]:
        """контакты вуза или вендора; сравниваются в Python по ФИО"""
        key = (owner, owner_id)
        if key not in self._contacts:
            column = (
                Contact.university_id if owner == "university" else Contact.vendor_id
            )
            self._contacts[key] = list(
                await self.session.scalars(select(Contact).where(column == owner_id))
            )
        return self._contacts[key]
