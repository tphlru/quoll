"""Вид листа и сопоставление колонок с полями (import-design §15.2)"""

import difflib
from dataclasses import dataclass, field

from quoll.imports import normalize
from quoll.imports.spec import KINDS, PERSONAL_HEADERS, FieldType, KindSpec

FUZZY_RATIO = 0.85
BY_VALUES_SHARE = 0.8
# для «по значениям» хватает начала листа
SAMPLE_ROWS = 200
_VALUE_TYPES = (
    FieldType.INN,
    FieldType.KPP,
    FieldType.EMAIL,
    FieldType.PHONE,
    FieldType.OKSO,
    FieldType.URL,
    FieldType.DATE,
)


@dataclass
class Mapped:
    kind: str | None
    mapping: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


def personal_columns(headers: list[str]) -> set[int]:
    return {
        i for i, h in enumerate(headers) if normalize.header_key(h) in PERSONAL_HEADERS
    }


def _names(spec) -> list[str]:
    """синонимы поля: подпись, ключ и перечисленные"""
    return [normalize.header_key(n) for n in (spec.label, spec.key, *spec.synonyms)]


def kind_by_name(name: str) -> str | None:
    key = normalize.header_key(name)
    for kind in KINDS.values():
        if key in (normalize.header_key(s) for s in kind.sheet_synonyms):
            return kind.key
    return None


def kind_by_headers(
    headers: list[str], extra: dict[str, list] | None = None
) -> str | None:
    """вид, у которого найдены все обязательные поля и не меньше двух полей,
    с наибольшим числом найденных; ничья или никто - None"""
    scored = []
    for kind in KINDS.values():
        found = set(
            columns(
                kind, headers, [], extra_fields=(extra or {}).get(kind.key)
            ).mapping.values()
        )
        required = {f.key for f in kind.fields if f.required}
        if required <= found and len(found) >= 2:
            scored.append((len(found), kind.key))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def detect(
    sheet_name: str,
    headers: list[str],
    rows: list[list[str]],
    single: bool,
    extra: dict[str, list] | None = None,
) -> Mapped:
    """в книге из нескольких листов вид - только по имени листа"""
    kind = kind_by_name(sheet_name)
    if kind is None and single:
        kind = kind_by_headers(headers, extra)
    if kind is None:
        return Mapped(None, notes=_personal_notes(headers))
    return columns(KINDS[kind], headers, rows, extra_fields=(extra or {}).get(kind))


def _personal_notes(headers: list[str]) -> dict[str, str]:
    return {str(i): "personal_data" for i in personal_columns(headers)}


def columns(
    kind: KindSpec,
    headers: list[str],
    rows: list[list[str]],
    *,
    extra_fields: list | None = None,
) -> Mapped:
    """колонки вида: точно, похоже, по значениям; ПДн - никогда"""
    fields = [*kind.fields, *(extra_fields or [])]
    result = Mapped(kind.key)
    personal = personal_columns(headers)
    taken: set[str] = set()
    keys = [normalize.header_key(h) for h in headers]
    for i in personal:
        result.notes[str(i)] = "personal_data"

    def assign(i: int, field_key: str, note: str | None) -> None:
        if field_key in taken:
            result.notes[str(i)] = "duplicate"
            return
        taken.add(field_key)
        result.mapping[str(i)] = field_key
        if note:
            result.notes[str(i)] = note

    # точные совпадения раньше похожих: иначе похожий занял бы чужое поле
    for i, key in enumerate(keys):
        if i in personal or not key:
            continue
        exact = next((f for f in fields if key in _names(f)), None)
        if exact is not None:
            assign(i, exact.key, None)
    for i, key in enumerate(keys):
        if (
            i in personal
            or not key
            or str(i) in result.mapping
            or str(i) in result.notes
        ):
            continue
        best, ratio = None, 0.0
        for f in fields:
            if f.key in taken:
                continue
            for name in _names(f):
                r = difflib.SequenceMatcher(None, key, name).ratio()
                if r > ratio:
                    best, ratio = f, r
        if best is not None and ratio >= FUZZY_RATIO:
            assign(i, best.key, "fuzzy")
    sample = rows[:SAMPLE_ROWS]
    for i in range(len(headers)):
        if i in personal or str(i) in result.mapping or str(i) in result.notes:
            continue
        values = [r[i] for r in sample if i < len(r) and r[i]]
        if values:
            for f in fields:
                if f.key in taken or f.type not in _VALUE_TYPES:
                    continue
                hits = sum(normalize.looks_like(f.type, v) for v in values)
                if hits / len(values) >= BY_VALUES_SHARE:
                    assign(i, f.key, "by_values")
                    break
        if str(i) not in result.mapping and str(i) not in result.notes:
            result.notes[str(i)] = "unused"
    return result
