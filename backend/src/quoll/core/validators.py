"""Проверки реквизитов, общие для схем API и разбора файлов импорта"""


def inn(value: str) -> str:
    """ИНН юрлица: 10 цифр и контрольная цифра"""
    if not (value.isdigit() and len(value) == 10):
        raise ValueError("INN must be 10 digits")
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    check = sum(int(d) * w for d, w in zip(value, weights, strict=False)) % 11 % 10
    if check != int(value[9]):
        raise ValueError("INN checksum does not match")
    return value


def kpp(value: str | None) -> str | None:
    if value is not None and not (value.isdigit() and len(value) == 9):
        raise ValueError("KPP must be 9 digits")
    return value
