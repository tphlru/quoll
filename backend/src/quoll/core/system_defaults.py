class SystemDefaults:
    """Тут будут дефолтные значения, чтобы не хардкодить по коду.

    сюда кладем то, что либо в нескольких модулях, либо
    задаёт поведение и переедет в настройки админа.

    в config.Settings лежит то, что задается окржением.
    Здесь же - то что запечатано в схему базы.
    """

    DEFAULT_MAX_SUBORDINATES = 5
    DEFAULT_MAX_ACTIVE_PROJECTS = 5

    # границы, в которых администратор может менять лимиты ёмкости
    MIN_CAPACITY_LIMIT = 1
    MAX_CAPACITY_LIMIT = 50

    # постраничная выдача списков
    DEFAULT_PAGE_SIZE = 50
    MAX_PAGE_SIZE = 100
    MIN_PAUSE_HOURS = 1
    MAX_PAUSE_HOURS = 720

    # отчёты: пороги спеки, их подтвердит нагрузочный тест
    REPORT_PARALLEL_LIMIT = 10
    REPORT_TIMEOUT_SECONDS = 300
    REPORT_UPLOAD_TIMEOUT_SECONDS = 60
    # лист xls - 65 536 строк, из них шапка (13), заголовки и итог
    REPORT_XLS_MAX_ROWS = 65_521
    REPORT_PDF_MAX_ROWS = 5_000
    REPORT_FILE_TTL_HOURS = 24

    # импорт: аренда применения продлевается на каждой единице
    IMPORT_LEASE_SECONDS = 120
    # попытки единицы при неожиданном сбое, потом FAILED - партия не застревает
    IMPORT_UNIT_ATTEMPTS = 3
    # отложенные взятия при недоступном Keycloak: 30 с x 2^n, не больше 30 мин
    IMPORT_IDP_RETRIES = 10
    IMPORT_IDP_BACKOFF_SECONDS = 30
    IMPORT_IDP_BACKOFF_MAX_SECONDS = 1800
    # пауза перед повтором единицы после сбоя
    IMPORT_UNIT_RETRY_SECONDS = 30
