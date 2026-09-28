"""Анализ партии: значения строк, ключи, части, статус (import-design §16).

Рабочие таблицы только читаются (M1); пишутся строки и листы партии. Тот же
код перепроверяет вид перед применением (M3): `analyze(..., upto=вид)`
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.auth.audit_models import TargetType
from quoll.catalog.models import Contact, ItDirection, ItProgram, Product, Specialty
from quoll.catalog.schemas import (
    ContactPatch,
    ContactWrite,
    DirectionPatch,
    DirectionWrite,
    ProductPatch,
    ProductWrite,
    ProgramPatch,
    ProgramWrite,
    SpecialtyPatch,
    SpecialtyWrite,
    UniversityPatch,
    UniversityWrite,
    VendorPatch,
    VendorWrite,
)
from quoll.config import settings
from quoll.core import validators
from quoll.imports import normalize
from quoll.imports.models import ImportBatch, ImportRow, ImportSheet, RowStatus
from quoll.imports.snapshot import Snapshot
from quoll.imports.spec import KINDS, REGISTRY, FieldSpec, FieldType, KindSpec, issue
from quoll.interactions.models import University, Vendor

NEW, UPDATE, SAME, MERGED = "NEW", "UPDATE", "SAME", "MERGED"
# связь-список id -> отношение модели
_RELATIONS = {"direction_ids": "directions", "product_ids": "products"}


@dataclass(frozen=True)
class PartDef:
    """запись, которую создаёт или меняет часть строки"""

    model: type
    target: TargetType
    write: type[BaseModel]
    patch: type[BaseModel]
    # поле файла -> атрибут модели
    attrs: dict[str, str]
    # атрибуты ключа и владельца: не сравниваются
    keys: tuple[str, ...]
    # списки, которые только пополняются (M4)
    lists: tuple[str, ...] = ()


PARTS: dict[str, PartDef] = {
    "direction": PartDef(
        ItDirection, TargetType.DIRECTION, DirectionWrite, DirectionPatch,
        {"name": "name"}, ("name",),
    ),
    "vendor": PartDef(
        Vendor, TargetType.VENDOR, VendorWrite, VendorPatch,
        {"name": "name", "site": "site", "kind": "kind"}, ("name",),
    ),
    "vendor_contact": PartDef(
        Contact, TargetType.CONTACT, ContactWrite, ContactPatch,
        {"contact_name": "full_name", "phone": "phone", "email": "email",
         "contact_methods": "contact_methods"},
        ("vendor_id", "full_name"), ("contact_methods",),
    ),
    "product": PartDef(
        Product, TargetType.PRODUCT, ProductWrite, ProductPatch,
        {"name": "name", "vendor": "vendor_id", "directions": "direction_ids",
         "description": "description", "url": "url"},
        ("name", "vendor_id"), ("direction_ids",),
    ),
    "program": PartDef(
        ItProgram, TargetType.PROGRAM, ProgramWrite, ProgramPatch,
        {"name": "name", "direction": "direction_id", "products": "product_ids",
         "description": "description", "url": "url", "site_course_id": "site_course_id"},
        ("name",), ("product_ids",),
    ),
    "specialty": PartDef(
        Specialty, TargetType.SPECIALTY, SpecialtyWrite, SpecialtyPatch,
        {"code": "code", "name": "name", "level": "level", "directions": "direction_ids",
         "tags": "tags"},
        ("code",), ("direction_ids", "tags"),
    ),
    "university": PartDef(
        University, TargetType.UNIVERSITY, UniversityWrite, UniversityPatch,
        {"full_name": "full_name", "short_name": "short_name", "inn": "inn", "kpp": "kpp",
         "region": "region", "city": "city", "site": "site"},
        ("inn", "kpp"),
    ),
    "contact": PartDef(
        Contact, TargetType.CONTACT, ContactWrite, ContactPatch,
        {"full_name": "full_name", "phone": "phone", "email": "email",
         "position": "position", "contact_methods": "contact_methods"},
        ("university_id", "full_name"), ("contact_methods",),
    ),
}  # fmt: skip


@dataclass
class Part:
    name: str
    key: str
    # существующая запись или None
    item: Any = None
    # атрибут -> значение из файла (ссылки - {"id"} или {"new_of_row", "part"})
    values: dict[str, Any] = field(default_factory=dict)
    # атрибут -> новое значение для update (списки - целиком после объединения)
    changes: dict[str, Any] = field(default_factory=dict)
    action: str = SAME
    # MERGED: строка и часть, в которую слита
    merged_into: "RowCtx | None" = None

    def target(self, kind: str) -> dict:
        out: dict[str, Any] = {"part": self.name, "kind": kind, "action": self.action}
        if self.item is not None:
            out["id"] = self.item.id
        if self.merged_into is not None:
            out["new_of_row"] = self.merged_into.row.id
        return out


@dataclass
class RowCtx:
    row: ImportRow
    sheet: ImportSheet
    kind: KindSpec
    values: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    diff: dict[str, list] = field(default_factory=dict)
    status: str = RowStatus.SAME
    group_key: str | None = None
    targets: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(i["level"] == "E" for i in self.issues)

    def add(self, code: str, field_key: str | None = None, **params) -> None:
        item = issue(code, field_key, **params)
        if item not in self.issues:
            self.issues.append(item)

    def part(self, name: str) -> Part | None:
        return next((p for p in self.parts if p.name == name), None)


@dataclass
class Analysis:
    batch: ImportBatch
    snap: Snapshot
    rows: list[RowCtx] = field(default_factory=list)
    # (вид ссылки, ключ) -> (строка, часть) NEW-записи партии (§16.5)
    new: dict[tuple[str, str], tuple[RowCtx, str]] = field(default_factory=dict)
    # (часть, ключ) -> строка, в которую сливаются повторы
    pending: dict[tuple[str, str], RowCtx] = field(default_factory=dict)
    # названия продуктов с листов продуктов - для проверки vendors.products
    planned_products: set[str] = field(default_factory=set)
    sheet_issues: dict[int, list[dict]] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[RowCtx]:
        return [r for r in self.rows if r.kind is not None and r.kind.key == kind]


def _key(*items) -> str:
    return json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)


def _ref_is_new(value) -> bool:
    return isinstance(value, dict) and "new_of_row" in value


def _plain(value):
    """значение для прогона схемы: ссылки на новые записи - заглушкой"""
    if isinstance(value, dict):
        return value.get("id", 0)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --- значения ячеек ---------------------------------------------------------


def _cells(ctx: RowCtx) -> dict[str, Any]:
    """сырые значения полей: колонка по mapping, поверх - правка"""
    raw = ctx.row.data.get("raw", [])
    out: dict[str, Any] = {}
    for index, key in ctx.sheet.mapping.items():
        i = int(index)
        if i < len(raw):
            out[key] = raw[i]
    out.update(ctx.row.data.get("edits") or {})
    return out


def _parse(ctx: RowCtx, fields: list[FieldSpec]) -> None:
    cells = _cells(ctx)
    for spec in fields:
        if spec.ignored:
            continue
        raw = cells.get(spec.key, "")
        if isinstance(raw, dict):
            # правка-ссылка: запись проверит разбор ссылок
            ctx.values[spec.key] = {"id": raw.get("id")}
            continue
        raw = str(raw).strip() if raw is not None else ""
        if not raw:
            ctx.values[spec.key] = None
            if spec.required:
                ctx.add("IMP-110", spec.key)
            continue
        try:
            if spec.type == FieldType.PHONE:
                value, ok = normalize.phone(raw)
                if not ok:
                    ctx.add("IMP-120", spec.key)
            elif spec.type == FieldType.REGION:
                value, exact = normalize.region(raw)
                if not exact:
                    ctx.add("IMP-123", spec.key)
            else:
                value = normalize.value_of(spec, raw)
        except normalize.NotValid as err:
            ctx.values[spec.key] = None
            ctx.add(err.code, spec.key)
            continue
        ctx.values[spec.key] = value


# --- ссылки ------------------------------------------------------------------


_REF_INDEX = {
    "direction": "directions",
    "vendor": "vendors",
    "program": "programs",
}


def _by_id(snap: Snapshot, ref: str, item_id) -> Any:
    if ref == "direction":
        return next(
            (d for items in snap.directions.values() for d in items if d.id == item_id),
            None,
        )
    if ref == "specialty":
        return next((s for s in snap.specialties.values() if s.id == item_id), None)
    return {
        "vendor": snap.vendors_by_id,
        "product": snap.products_by_id,
        "program": snap.programs_by_id,
        "university": snap.universities_by_id,
    }[ref].get(item_id)


def _label(item) -> str:
    for attr in ("short_name", "name", "code"):
        if hasattr(item, attr):
            return str(getattr(item, attr))
    return str(item.id)


def _new_ref(an: Analysis, ref: str, key: str) -> dict | None:
    found = an.new.get((ref, key))
    if found is None:
        return None
    ctx, part = found
    return {"new_of_row": ctx.row.id, "part": part}


def resolve(an: Analysis, ctx: RowCtx, spec: FieldSpec, text: str) -> tuple[Any, str]:
    """ссылка по тексту ячейки -> ({"id"} | {"new_of_row"}, подпись);
    не нашлась - NotValid с кодом"""
    snap = an.snap
    if spec.ref == "university":
        return _university_ref(an, text)
    if spec.ref == "specialty":
        code = text.strip()
        if code in snap.specialties:
            return {"id": snap.specialties[code].id}, code
        new = _new_ref(an, "specialty", code)
        if new:
            return new, code
        raise normalize.NotValid()
    key = normalize.text_key(text)
    if spec.ref == "product":
        found = snap.products.get(key, [])
        new = _new_ref(an, "product", key)
        if len(found) + (new is not None) > 1:
            raise normalize.NotValid("IMP-111")
        if found:
            return {"id": found[0].id}, found[0].name
        if new:
            return new, text
        raise normalize.NotValid()
    found = getattr(snap, _REF_INDEX[spec.ref]).get(key, [])
    if len(found) > 1:
        raise normalize.NotValid("IMP-111")
    if found:
        return {"id": found[0].id}, found[0].name
    new = _new_ref(an, spec.ref, key)
    if new:
        return new, text
    raise normalize.NotValid()


def _university_ref(an: Analysis, text: str) -> tuple[Any, str]:
    """ИНН с верной суммой, иначе краткое, затем полное название (§16.3)"""
    snap = an.snap
    digits = re.sub(r"\D", "", text)
    try:
        inn = validators.inn(digits) if len(digits) == 10 else None
    except ValueError:
        inn = None
    if inn:
        found = [u for u in snap.universities if u.inn == inn]
        new_key = ("university_inn", inn)
    else:
        key = normalize.text_key(text)
        found = [
            u for u in snap.universities if normalize.text_key(u.short_name) == key
        ]
        if not found:
            found = [
                u for u in snap.universities if normalize.text_key(u.full_name) == key
            ]
        new_key = ("university_name", key)
    if len(found) > 1:
        raise normalize.NotValid("IMP-111")
    if found:
        return {"id": found[0].id}, found[0].short_name
    new = _new_ref(an, *new_key)
    if new:
        return new, text
    raise normalize.NotValid("IMP-006")


def _resolve_field(an: Analysis, ctx: RowCtx, spec: FieldSpec) -> None:
    """ячейка-ссылка (или список) -> ссылки в values, подписи в labels"""
    value = ctx.values.get(spec.key)
    if value is None or value == []:
        return
    items = value if spec.type == FieldType.LIST_REF else [value]
    refs, labels = [], []
    for item in items:
        if isinstance(item, dict):
            # правка {"id"}: запись должна быть того же вида
            found = _by_id(an.snap, spec.ref, item.get("id"))
            if found is None:
                ctx.add("IMP-110", spec.key)
                ctx.values[spec.key] = None
                return
            refs.append({"id": found.id})
            labels.append(_label(found))
            continue
        try:
            ref, label = resolve(an, ctx, spec, item)
        except normalize.NotValid as err:
            ctx.add(err.code, spec.key)
            ctx.values[spec.key] = None
            return
        if ref not in refs:
            refs.append(ref)
            labels.append(label)
    ctx.values[spec.key] = refs if spec.type == FieldType.LIST_REF else refs[0]
    ctx.labels[spec.key] = "; ".join(labels)


# --- части -------------------------------------------------------------------


def _current(item, attr: str):
    if attr in _RELATIONS:
        return [x.id for x in getattr(item, _RELATIONS[attr])]
    return getattr(item, attr)


def _changes(defn: PartDef, part: Part) -> dict[str, Any]:
    """непустые значения файла, отличные от базы; списки - объединение"""
    out = {}
    for attr, value in part.values.items():
        if attr in defn.keys or value is None or value == []:
            continue
        old = _current(part.item, attr)
        if attr in defn.lists or attr in _RELATIONS:
            old = list(old or [])
            ids = [v["id"] if isinstance(v, dict) and "id" in v else v for v in value]
            added = [v for v in ids if v not in old]
            if added:
                out[attr] = old + added
        elif isinstance(value, dict):
            if value.get("id") != old:
                out[attr] = value.get("id") if "id" in value else value
        elif value != old:
            out[attr] = value
    return out


def _check(ctx: RowCtx, defn: PartDef, part: Part, fields: dict[str, str]) -> None:
    """NEW - схема *Write, UPDATE - *Patch (§16.4)"""
    if part.action == NEW:
        data = {a: _plain(v) for a, v in part.values.items() if v is not None}
        schema = defn.write
    else:
        data = {a: _plain(v) for a, v in part.changes.items()}
        schema = defn.patch
    try:
        schema.model_validate(data)
    except ValidationError as err:
        back = {a: f for f, a in fields.items()}
        for e in err.errors():
            attr = str(e["loc"][0]) if e["loc"] else None
            ctx.add("IMP-110", back.get(attr, attr))


def _settle(ctx: RowCtx, defn: PartDef, part: Part) -> None:
    if part.item is None:
        part.action = NEW
    else:
        part.changes = _changes(defn, part)
        part.action = UPDATE if part.changes else SAME


def _add_part(
    an: Analysis,
    ctx: RowCtx,
    name: str,
    key: str,
    item,
    values: dict[str, Any],
    new_keys: list[tuple[str, str]] = (),
) -> Part:
    """часть строки: сравнение с базой, повторы ключа в партии (§16.4-16.5)"""
    defn = PARTS[name]
    part = Part(name=name, key=key, item=item, values=values)
    ctx.parts.append(part)
    first = an.pending.get((name, key))
    if first is not None and first is not ctx:
        other = first.part(name)
        clash = [
            a
            for a, v in values.items()
            if a not in defn.lists
            and a not in _RELATIONS
            and v is not None
            and other.values.get(a) is not None
            and other.values[a] != v
        ]
        if clash:
            ctx.add("IMP-141", None, ref_row=first.row.number)
            first.add("IMP-141", None, ref_row=ctx.row.number)
            return part
        for a, v in values.items():
            if v is None or v == []:
                continue
            if a in defn.lists or a in _RELATIONS:
                merged = list(other.values.get(a) or [])
                merged += [x for x in v if x not in merged]
                other.values[a] = merged
            elif other.values.get(a) is None:
                other.values[a] = v
        _settle(first, defn, other)
        part.action = MERGED
        part.merged_into = first
        ctx.add("IMP-140", None, ref_row=first.row.number)
        return part
    _settle(ctx, defn, part)
    if part.action in (NEW, UPDATE) and not ctx.failed:
        an.pending[(name, key)] = ctx
        if part.action == NEW:
            for new_key in new_keys:
                an.new.setdefault(new_key, (ctx, name))
    return part


def _v(ctx: RowCtx, spec: KindSpec, pairs: dict[str, str]) -> dict[str, Any]:
    """значения части: поле файла -> атрибут; ссылки как есть"""
    return {attr: ctx.values.get(f) for f, attr in pairs.items() if spec.field(f)}


def _owner_id(ref) -> Any:
    return ref.get("id") if isinstance(ref, dict) and "id" in ref else None


async def _contact_part(
    an: Analysis, ctx: RowCtx, name: str, owner: str, owner_ref, full_name: str
) -> None:
    defn = PARTS[name]
    owner_attr = "vendor_id" if owner == "vendor" else "university_id"
    fields = {f: a for f, a in defn.attrs.items() if a != "full_name"}
    values = {a: ctx.values.get(f) for f, a in fields.items()}
    values["full_name"] = full_name
    values[owner_attr] = owner_ref
    key = _key(owner_ref, normalize.text_key(full_name))
    item = None
    owner_id = _owner_id(owner_ref)
    if owner_id is not None:
        found = [
            c
            for c in await an.snap.contacts(owner, owner_id)
            if normalize.text_key(c.full_name) == normalize.text_key(full_name)
        ]
        if len(found) > 1:
            ctx.add("IMP-111", "full_name")
            return
        item = found[0] if found else None
    part = _add_part(an, ctx, name, key, item, values)
    _check(ctx, defn, part, defn.attrs)


# --- виды --------------------------------------------------------------------


async def _directions(an: Analysis, ctx: RowCtx) -> None:
    name = ctx.values.get("name")
    if not name:
        return
    key = normalize.text_key(name)
    found = an.snap.directions.get(key, [])
    part = _add_part(
        an, ctx, "direction", key, found[0] if found else None, {"name": name},
        [("direction", key)],
    )  # fmt: skip
    _check(ctx, PARTS["direction"], part, PARTS["direction"].attrs)


async def _vendors(an: Analysis, ctx: RowCtx) -> None:
    name = ctx.values.get("name")
    if not name:
        return
    key = normalize.text_key(name)
    found = an.snap.vendors.get(key, [])
    defn = PARTS["vendor"]
    part = _add_part(
        an, ctx, "vendor", key, found[0] if found else None,
        _v(ctx, ctx.kind, {"name": "name", "site": "site", "kind": "kind"}),
        [("vendor", key)],
    )  # fmt: skip
    _check(ctx, defn, part, defn.attrs)
    vendor = found[0] if found else None
    # продукты вендора только сверяются: создать продукт отсюда нельзя
    for product in ctx.values.get("products") or []:
        pkey = normalize.text_key(product)
        same = [
            p
            for p in an.snap.products.get(pkey, [])
            if vendor and p.vendor_id == vendor.id
        ]
        if same or pkey in an.planned_products:
            continue
        code = "IMP-121" if an.snap.products.get(pkey) else "IMP-122"
        ctx.add(code, "products")
    contact = ctx.values.get("contact_name")
    if contact:
        if part.action == MERGED:
            owner = {"new_of_row": part.merged_into.row.id, "part": "vendor"}
            first = part.merged_into.part("vendor")
            if first.item is not None:
                owner = {"id": first.item.id}
        elif vendor is not None:
            owner = {"id": vendor.id}
        else:
            owner = {"new_of_row": ctx.row.id, "part": "vendor"}
        await _contact_part(an, ctx, "vendor_contact", "vendor", owner, contact)


async def _products(an: Analysis, ctx: RowCtx) -> None:
    name, vendor = ctx.values.get("name"), ctx.values.get("vendor")
    if not name or not vendor:
        return
    key = normalize.text_key(name)
    vendor_id = _owner_id(vendor)
    found = [p for p in an.snap.products.get(key, []) if p.vendor_id == vendor_id]
    defn = PARTS["product"]
    values = _v(ctx, ctx.kind, defn.attrs)
    part = _add_part(
        an, ctx, "product", _key(key, vendor), found[0] if found else None, values,
        [("product", key)],
    )  # fmt: skip
    _check(ctx, defn, part, defn.attrs)


async def _programs(an: Analysis, ctx: RowCtx) -> None:
    name = ctx.values.get("name")
    if not name:
        return
    key = normalize.text_key(name)
    found = an.snap.programs.get(key, [])
    defn = PARTS["program"]
    values = _v(ctx, ctx.kind, defn.attrs)
    part = _add_part(
        an, ctx, "program", key, found[0] if found else None, values, [("program", key)]
    )
    _check(ctx, defn, part, defn.attrs)


async def _specialties(an: Analysis, ctx: RowCtx) -> None:
    code = ctx.values.get("code")
    if not code:
        return
    defn = PARTS["specialty"]
    values = _v(ctx, ctx.kind, defn.attrs)
    part = _add_part(
        an, ctx, "specialty", code, an.snap.specialties.get(code), values,
        [("specialty", code)],
    )  # fmt: skip
    _check(ctx, defn, part, defn.attrs)


async def _universities(an: Analysis, ctx: RowCtx) -> None:
    inn, kpp = ctx.values.get("inn"), ctx.values.get("kpp")
    if not inn:
        return
    same_inn = [u for u in an.snap.universities if u.inn == inn]
    if kpp:
        found = [u for u in same_inn if u.kpp == kpp]
    elif len(same_inn) > 1:
        ctx.add("IMP-111", "kpp")
        return
    else:
        found = same_inn
    defn = PARTS["university"]
    values = _v(ctx, ctx.kind, defn.attrs)
    new_keys = [("university_inn", inn)]
    for f in ("short_name", "full_name"):
        if ctx.values.get(f):
            new_keys.append(("university_name", normalize.text_key(ctx.values[f])))
    part = _add_part(
        an, ctx, "university", _key(inn, kpp or (found[0].kpp if found else None)),
        found[0] if found else None, values, new_keys,
    )  # fmt: skip
    _check(ctx, defn, part, defn.attrs)


async def _university_specialties(an: Analysis, ctx: RowCtx) -> None:
    university, specialty = ctx.values.get("university"), ctx.values.get("code")
    if not university or not specialty:
        return
    pair = (_owner_id(university), _owner_id(specialty))
    key = _key(university, specialty)
    first = an.pending.get(("university_specialty", key))
    part = Part("university_specialty", key, values={"pair": [university, specialty]})
    ctx.parts.append(part)
    if first is not None:
        part.action, part.merged_into = MERGED, first
        ctx.add("IMP-140", None, ref_row=first.row.number)
    elif None not in pair and pair in an.snap.university_specialties:
        part.action = SAME
    else:
        part.action = NEW
        an.pending[("university_specialty", key)] = ctx


async def _contacts(an: Analysis, ctx: RowCtx) -> None:
    university, name = ctx.values.get("university"), ctx.values.get("full_name")
    if not university or not name:
        return
    await _contact_part(an, ctx, "contact", "university", university, name)


_KINDS = {
    "directions": _directions,
    "vendors": _vendors,
    "products": _products,
    "programs": _programs,
    "specialties": _specialties,
    "universities": _universities,
    "university_specialties": _university_specialties,
    "contacts": _contacts,
}


# --- ход ---------------------------------------------------------------------


def _status(ctx: RowCtx) -> str:
    if ctx.row.excluded or ctx.kind is None:
        return RowStatus.EXCLUDED
    levels = {i["level"] for i in ctx.issues}
    if "E" in levels:
        return RowStatus.ERROR
    if "C" in levels:
        return RowStatus.CONFLICT
    if any(p.action == NEW for p in ctx.parts):
        return RowStatus.NEW
    if any(p.action == UPDATE for p in ctx.parts):
        return RowStatus.UPDATE
    return RowStatus.SAME


def _show(an: Analysis, attr: str, value) -> Any:
    """значение diff для экрана: id связей -> названия"""
    if attr in _RELATIONS or attr in ("direction_id", "vendor_id"):
        ref = {"direction_ids": "direction", "product_ids": "product",
               "direction_id": "direction", "vendor_id": "vendor"}[attr]  # fmt: skip
        ids = value if isinstance(value, list) else [value]
        names = []
        for v in ids:
            item = _by_id(an.snap, ref, v) if not isinstance(v, dict) else None
            names.append(_label(item) if item is not None else "*")
        return names if isinstance(value, list) else names[0]
    return value


def _finish(an: Analysis, ctx: RowCtx) -> None:
    ctx.diff = {}
    for part in ctx.parts:
        if part.action != UPDATE:
            continue
        back = {a: f for f, a in PARTS[part.name].attrs.items()}
        for attr, new in part.changes.items():
            old = _current(part.item, attr)
            ctx.diff[back.get(attr, attr)] = [
                _show(an, attr, old),
                _show(an, attr, new),
            ]
    ctx.targets = [p.target(ctx.kind.key) for p in ctx.parts] if ctx.kind else []
    ctx.status = _status(ctx)


def digest(ctx: RowCtx) -> str:
    payload = [ctx.status, ctx.issues, ctx.targets, ctx.group_key, ctx.values, ctx.diff]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def load(
    session: AsyncSession, batch_id: int
) -> tuple[list[ImportSheet], list[ImportRow]]:
    sheets = list(
        await session.scalars(
            select(ImportSheet)
            .where(ImportSheet.batch_id == batch_id)
            .order_by(ImportSheet.number)
        )
    )
    rows = list(
        await session.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch_id)
            .order_by(ImportRow.sheet_id, ImportRow.number)
        )
    )
    return sheets, rows


async def analyze(
    session: AsyncSession, batch: ImportBatch, *, upto: str | None = None
) -> Analysis:
    """анализ в памяти; upto - остановиться после этого вида (применение)"""
    snap = await Snapshot.load(session)
    an = Analysis(batch=batch, snap=snap)
    sheets, rows = await load(session, batch.id)
    by_sheet: dict[int, list[ImportRow]] = {}
    for row in rows:
        by_sheet.setdefault(row.sheet_id, []).append(row)
    ordered = sorted(
        (s for s in sheets if s.kind in KINDS),
        key=lambda s: (KINDS[s.kind].order, s.number),
    )
    for sheet in sheets:
        an.sheet_issues[sheet.id] = _sheet_issues(sheet, by_sheet.get(sheet.id, []))
        if sheet.kind is None and upto is None:
            # строки пропущенного листа не применяются
            an.rows += [
                RowCtx(row=r, sheet=sheet, kind=None)
                for r in by_sheet.get(sheet.id, [])
            ]
    for sheet in ordered:
        if sheet.kind != "products":
            continue
        for row in by_sheet.get(sheet.id, []):
            ctx = RowCtx(row=row, sheet=sheet, kind=KINDS["products"])
            name = _cells(ctx).get("name")
            if isinstance(name, str) and name.strip() and not row.excluded:
                an.planned_products.add(normalize.text_key(name))
    for sheet in ordered:
        kind = KINDS[sheet.kind]
        if upto is not None and kind.order > KINDS[upto].order:
            break
        for row in by_sheet.get(sheet.id, []):
            ctx = RowCtx(row=row, sheet=sheet, kind=kind)
            an.rows.append(ctx)
            _parse(ctx, list(kind.fields))
            if row.excluded or kind.key == REGISTRY:
                continue
            for spec in kind.fields:
                # продукты вендора только сверяются (IMP-121/122), не ищутся
                if kind.key == "vendors" and spec.key == "products":
                    continue
                if spec.type in (FieldType.REF, FieldType.LIST_REF):
                    _resolve_field(an, ctx, spec)
            await _KINDS[kind.key](an, ctx)
    if upto is None or upto == REGISTRY:
        from quoll.imports import registry

        await registry.run(session, an, an.of_kind(REGISTRY))
    for ctx in an.rows:
        _finish(an, ctx)
    return an


def _sheet_issues(sheet: ImportSheet, rows: list[ImportRow]) -> list[dict]:
    if sheet.kind is None:
        return [issue("IMP-101")]
    if sheet.kind == REGISTRY and len(rows) > settings.import_max_registry_rows:
        return [issue("IMP-104", limit=settings.import_max_registry_rows)]
    return []


async def run(session: AsyncSession, batch: ImportBatch) -> bool:
    """полный пересчёт партии: пишутся только строки с новым digest (§16.12);
    version растёт, только если что-то изменилось. Вернёт, изменилось ли"""
    an = await analyze(session, batch)
    changed = False
    for ctx in an.rows:
        changed |= _store(ctx)
    sheets = {ctx.sheet.id: ctx.sheet for ctx in an.rows}
    sheets |= {
        s.id: s
        for s in await session.scalars(
            select(ImportSheet).where(ImportSheet.batch_id == batch.id)
        )
    }
    for sheet in sheets.values():
        found = an.sheet_issues.get(sheet.id, [])
        if sheet.issues != found:
            sheet.issues = found
            changed = True
    summary = {**(batch.summary or {}), **_summary(list(sheets.values()), an.rows)}
    if summary != batch.summary:
        batch.summary = summary
    if changed:
        batch.version += 1
    return changed


def _store(ctx: RowCtx) -> bool:
    row = ctx.row
    new_digest = digest(ctx)
    if row.digest == new_digest:
        return False
    row.digest = new_digest
    row.status = ctx.status
    row.issues = ctx.issues
    row.targets = ctx.targets
    row.group_key = ctx.group_key
    row.data = {
        **row.data,
        "values": ctx.values,
        "labels": ctx.labels,
        "diff": ctx.diff,
    }
    return True


def _summary(sheets: list[ImportSheet], rows: list[RowCtx]) -> dict:
    by_sheet: dict[int, dict[str, int]] = {s.number: {} for s in sheets}
    statuses: dict[str, int] = {}
    for ctx in rows:
        counts = by_sheet[ctx.sheet.number]
        counts[ctx.status] = counts.get(ctx.status, 0) + 1
        statuses[ctx.status] = statuses.get(ctx.status, 0) + 1
    return {
        "sheets": {str(k): v for k, v in sorted(by_sheet.items())},
        "statuses": statuses,
    }
