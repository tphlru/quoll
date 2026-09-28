"""Ошибки ручек импорта с кодами IMP-0xx (import-design §22.1)"""

from quoll.core.exceptions import AppException


def import_error(status: int, code: str, message: str, **params) -> AppException:
    return AppException(status, message, code, params or None)


def unreadable() -> AppException:
    # текст исключения библиотеки наружу не отдаём (errors-ru §3 п. 6)
    return import_error(422, "IMP-001", "Cannot read the file as a table")


def wrong_status(status: str) -> AppException:
    return import_error(409, "IMP-010", f"Import is {status}", status=status)
