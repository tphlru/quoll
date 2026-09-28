"""Нормализация значений ячеек по типу поля (import-design §15.3).

Функции возвращают значение или бросают NotValid с кодом замечания; тексты
ячеек в исключение не попадают (M6)
"""

import csv
import difflib
import re
from datetime import date, datetime, timedelta

from quoll.catalog.regions import REGIONS
from quoll.core import validators
from quoll.imports.spec import BOOLS, FieldSpec, FieldType

_QUOTES = "«»„“”\"'"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OKSO = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")
_EXCEL_EPOCH = date(1899, 12, 30)


class NotValid(ValueError):
    def __init__(self, code: str = "IMP-110"):
        super().__init__(code)
        self.code = code


def text_key(value: str) -> str:
    """ключ названия: регистр, ё, кавычки, пробелы, точка в конце не важны"""
    value = value.casefold().replace("ё", "е")
    value = value.translate({ord(q): None for q in _QUOTES})
    value = " ".join(value.split())
    return value.rstrip(".")


def header_key(value: str) -> str:
    """нормализованный заголовок колонки или имя листа"""
    value = value.casefold().replace("ё", "е").replace("*", "")
    value = re.sub(r"\([^)]*\)", "", value)
    value = value.translate({ord(q): None for q in _QUOTES})
    return " ".join(value.split()).strip(" .,:;-")


def split_list(value: str, seps: str) -> list[str]:
    """запятая режет только вне кавычек"""
    if not value:
        return []
    parts = [value]
    for sep in seps:
        if sep == ",":
            parts = [
                item
                for part in parts
                for item in next(csv.reader([part], skipinitialspace=True))
            ]
        else:
            parts = [item for part in parts for item in part.split(sep)]
    out = []
    for item in parts:
        item = item.strip().strip(_QUOTES).strip()
        if item and item not in out:
            out.append(item)
    return out


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # excel-серия - только там, где тип не сохранился (csv, json)
    if value.isdigit() and 1 <= int(value) <= 80000:
        return _EXCEL_EPOCH + timedelta(days=int(value))
    raise NotValid()


def phone(value: str) -> tuple[str, bool]:
    """(значение, распознан ли)"""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:], True
    if len(digits) == 10:
        return "+7" + digits, True
    return value.strip(), False


def region(value: str) -> tuple[str, bool]:
    """(регион из справочника, точное ли совпадение)"""
    key = text_key(value)
    by_key = {text_key(r): r for r in REGIONS}
    if key in by_key:
        return by_key[key], True
    best = max(by_key, key=lambda k: difflib.SequenceMatcher(None, key, k).ratio())
    if difflib.SequenceMatcher(None, key, best).ratio() >= 0.9:
        return by_key[best], False
    raise NotValid()


def enum(spec: FieldSpec, value: str) -> str:
    found = spec.enum_values.get(text_key(value))
    if found is None:
        raise NotValid()
    return found


def boolean(value: str) -> bool:
    found = BOOLS.get(text_key(value))
    if found is None:
        raise NotValid()
    return found


def value_of(spec: FieldSpec, raw: str):
    """значение простого типа; ссылки разбирает анализ, здесь только текст"""
    raw = raw.strip()
    t = spec.type
    if t in (FieldType.TEXT, FieldType.URL, FieldType.REF):
        return raw
    if t in (FieldType.LIST, FieldType.LIST_REF):
        return split_list(raw, spec.list_sep)
    if t == FieldType.INT:
        if not re.fullmatch(r"-?\d+", raw):
            raise NotValid()
        return int(raw)
    if t == FieldType.DATE:
        return parse_date(raw).isoformat()
    if t == FieldType.BOOL:
        return boolean(raw)
    if t == FieldType.ENUM:
        return enum(spec, raw)
    if t == FieldType.INN:
        digits = re.sub(r"\D", "", raw)
        try:
            return validators.inn(digits)
        except ValueError:
            raise NotValid() from None
    if t == FieldType.KPP:
        digits = re.sub(r"\D", "", raw)
        try:
            return validators.kpp(digits)
        except ValueError:
            raise NotValid() from None
    if t == FieldType.EMAIL:
        if not _EMAIL.match(raw.lower()):
            raise NotValid()
        return raw.lower()
    if t == FieldType.OKSO:
        if not _OKSO.match(raw):
            raise NotValid()
        return raw
    # телефон и регион отдают ещё и признак точности - их зовут отдельно
    return raw


# образцы значений для сопоставления колонок «по значениям» (§15.2)
def looks_like(t: FieldType, raw: str) -> bool:
    raw = raw.strip()
    try:
        if t == FieldType.INN:
            validators.inn(re.sub(r"\D", "", raw))
            return True
        if t == FieldType.KPP:
            return bool(re.fullmatch(r"\d{9}", raw))
        if t == FieldType.EMAIL:
            return bool(_EMAIL.match(raw.lower()))
        if t == FieldType.PHONE:
            return len(re.sub(r"\D", "", raw)) >= 10
        if t == FieldType.OKSO:
            return bool(_OKSO.match(raw))
        if t == FieldType.URL:
            return bool(
                re.match(r"^(https?://)?[\w.-]+\.[a-zа-я]{2,}(/\S*)?$", raw, re.I)
            )
        if t == FieldType.DATE:
            parse_date(raw)
            return not raw.isdigit()
    except (ValueError, NotValid):
        return False
    return False
