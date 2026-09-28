"""Реестр обработчиков доп. шагов. Листовой модуль - ничего не импортирует
из quoll, чтобы workflows не зависел от interactions"""

SUPPLEMENTARY_AGREEMENT = "SUPPLEMENTARY_AGREEMENT"

HANDLERS: dict[str, str] = {SUPPLEMENTARY_AGREEMENT: "Допсоглашение"}

for _code, _label in HANDLERS.items():
    assert _code and len(_code) <= 40, _code
