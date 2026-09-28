"""Что понимает импорт: виды листов, поля, синонимы, коды замечаний
(import-design §14, §22). Единственный источник описания формата: из него
строятся ручка видов, шаблон xlsx и docs/import-format.md (В20)"""

from dataclasses import dataclass, field
from enum import StrEnum


class FieldType(StrEnum):
    TEXT = "text"
    INT = "int"
    DATE = "date"
    BOOL = "bool"
    LIST = "list"
    REF = "ref"
    LIST_REF = "list_ref"
    ENUM = "enum"
    INN = "inn"
    KPP = "kpp"
    PHONE = "phone"
    EMAIL = "email"
    URL = "url"
    OKSO = "okso"
    REGION = "region"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = False
    synonyms: tuple[str, ...] = ()
    description: str = ""
    example: str = ""
    # договорное: у существующих записей не перезаписывается (ответ 24)
    contract: bool = False
    # поле заявки, одинаковое во всех строках вуза (реестр)
    group: bool = False
    list_sep: str = ";"
    # синоним значения -> значение
    enum_values: dict[str, str] = field(default_factory=dict)
    # доп. поле реестра (§4.8)
    extra: bool = False
    # распознаётся, но не импортируется (В17)
    ignored: bool = False
    # на что ссылается ref / list_ref
    ref: str = ""


@dataclass(frozen=True)
class KindSpec:
    key: str
    label: str
    order: int
    sheet_synonyms: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    # записи, которые создаёт или меняет строка
    parts: tuple[str, ...]

    def field(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)


F = FieldType

_LEVELS = {
    "бакалавриат": "BACHELOR",
    "bachelor": "BACHELOR",
    "специалитет": "SPECIALIST",
    "specialist": "SPECIALIST",
    "магистратура": "MASTER",
    "master": "MASTER",
}
_TRANSFER = {
    "передано": "TRANSFERRED",
    "передан": "TRANSFERRED",
    "да": "TRANSFERRED",
    "transferred": "TRANSFERRED",
    "не передано": "NOT_TRANSFERRED",
    "не передан": "NOT_TRANSFERRED",
    "нет": "NOT_TRANSFERRED",
    "not_transferred": "NOT_TRANSFERRED",
}
_STATUS = {
    "в работе": "IN_WORK",
    "подписан": "SIGNED",
    "на паузе": "PAUSED",
    "завершено": "DONE",
    "завершена": "DONE",
    "отказ": "REFUSED",
}
BOOLS = {"да": True, "yes": True, "true": True, "1": True, "+": True,
         "нет": False, "no": False, "false": False, "0": False, "-": False}  # fmt: skip

DIRECTIONS = KindSpec(
    key="directions",
    label="ИТ-направления",
    order=1,
    sheet_synonyms=("направления", "ит-направления", "directions"),
    fields=(
        FieldSpec("name", "Название", required=True, synonyms=("направление",),
                  example="Разработка"),
    ),
    parts=("direction",),
)  # fmt: skip

VENDORS = KindSpec(
    key="vendors",
    label="Вендоры",
    order=2,
    sheet_synonyms=("вендоры", "vendors", "компании"),
    fields=(
        FieldSpec("name", "Название", required=True,
                  synonyms=("компания", "вендор", "название компании"), example="ПАО «Ростелеком»"),
        FieldSpec("site", "Сайт", F.URL, synonyms=("сайт компании",), example="rt.ru"),
        FieldSpec("kind", "Вид", synonyms=("тип",), example="Дочерняя"),
        FieldSpec("contact_name", "ФИО контакта", synonyms=("фио", "контакт", "контактное лицо"),
                  example="Иванов Иван Иванович"),
        FieldSpec("phone", "Телефон", F.PHONE, synonyms=("телефон контакта",),
                  example="+7 900 123-45-67"),
        FieldSpec("email", "Email", F.EMAIL, synonyms=("почта", "e-mail", "электронная почта"),
                  example="ivanov@rt.ru"),
        FieldSpec("contact_methods", "Способы связи", F.LIST, list_sep=";,",
                  synonyms=("способ связи",), example="почта; телефон"),
        FieldSpec("products", "Продукты", F.LIST_REF, list_sep=";,", ref="product",
                  synonyms=("продукт", "по"),
                  description="только связывает с уже известными продуктами"),
    ),
    parts=("vendor", "vendor_contact"),
)  # fmt: skip

PRODUCTS = KindSpec(
    key="products",
    label="ИТ-продукты",
    order=3,
    sheet_synonyms=("продукты", "ит-продукты", "products", "по"),
    fields=(
        FieldSpec("name", "Название", required=True, synonyms=("продукт", "по"),
                  example="RT.DataLake"),
        FieldSpec("vendor", "Вендор", F.REF, required=True, ref="vendor",
                  synonyms=("компания",), example="ПАО «Ростелеком»"),
        FieldSpec("directions", "Направления", F.LIST_REF, required=True, ref="direction",
                  synonyms=("направление", "ит-направления"), example="Аналитика; Разработка"),
        FieldSpec("description", "Описание", example="Хранение и обработка данных"),
        FieldSpec("url", "Ссылка", F.URL, synonyms=("сайт",), example="https://data.rt.ru"),
    ),
    parts=("product",),
)  # fmt: skip

PROGRAMS = KindSpec(
    key="programs",
    label="ИТ-программы",
    order=4,
    sheet_synonyms=("программы", "ит-программы", "programs", "курсы"),
    fields=(
        FieldSpec("name", "Название", required=True, synonyms=("программа", "курс"),
                  example="DevOps-инженер с нуля"),
        FieldSpec("direction", "Направление", F.REF, required=True, ref="direction",
                  synonyms=("ит-направление",), example="Разработка"),
        FieldSpec("products", "Продукты", F.LIST_REF, ref="product",
                  synonyms=("продукт",), example="Basis Dynamix"),
        FieldSpec("description", "Описание"),
        FieldSpec("url", "Ссылка", F.URL, synonyms=("сайт",)),
        FieldSpec("site_course_id", "ID курса на сайте", synonyms=("id курса",)),
    ),
    parts=("program",),
)  # fmt: skip

SPECIALTIES = KindSpec(
    key="specialties",
    label="Специальности",
    order=5,
    sheet_synonyms=("специальности", "specialties"),
    fields=(
        FieldSpec("code", "Код ОКСО", F.OKSO, required=True, synonyms=("код",),
                  example="09.03.01"),
        FieldSpec("name", "Название", required=True, synonyms=("специальность",),
                  example="Информатика и вычислительная техника"),
        FieldSpec("level", "Уровень", F.ENUM, required=True, enum_values=_LEVELS,
                  example="Бакалавриат"),
        FieldSpec("directions", "Направления", F.LIST_REF, ref="direction",
                  example="Разработка; Аналитика"),
        FieldSpec("tags", "Теги навыков", F.LIST, synonyms=("теги",),
                  example="программирование; базы данных"),
    ),
    parts=("specialty",),
)  # fmt: skip

UNIVERSITIES = KindSpec(
    key="universities",
    label="Вузы",
    order=6,
    sheet_synonyms=("вузы", "universities", "университеты"),
    fields=(
        FieldSpec("full_name", "Полное название", required=True,
                  synonyms=("полное наименование",),
                  example="Федеральное государственное ... «Тульский государственный университет»"),
        FieldSpec("short_name", "Краткое название", required=True,
                  synonyms=("краткое наименование", "вуз"), example="ТулГУ"),
        FieldSpec("inn", "ИНН", F.INN, required=True, example="7106003011"),
        FieldSpec("kpp", "КПП", F.KPP, example="710601001"),
        FieldSpec("region", "Регион", F.REGION, required=True, example="Тульская область"),
        FieldSpec("city", "Город", required=True, example="Тула"),
        FieldSpec("site", "Сайт", F.URL, example="tulsu.ru"),
    ),
    parts=("university",),
)  # fmt: skip

UNIVERSITY_SPECIALTIES = KindSpec(
    key="university_specialties",
    label="Специальности вузов",
    order=7,
    sheet_synonyms=("специальности вузов", "university specialties"),
    fields=(
        FieldSpec("university", "Вуз", F.REF, required=True, ref="university",
                  synonyms=("вуз (краткое название)", "краткое название", "инн вуза"),
                  example="ТулГУ"),
        FieldSpec("code", "Код ОКСО", F.REF, required=True, ref="specialty",
                  synonyms=("код",), example="09.03.01"),
    ),
    parts=("university_specialty",),
)  # fmt: skip

CONTACTS = KindSpec(
    key="contacts",
    label="Контакты вузов",
    order=8,
    sheet_synonyms=("контакты", "контакты вузов", "contacts"),
    fields=(
        FieldSpec("university", "Вуз", F.REF, required=True, ref="university",
                  synonyms=("инн вуза",), example="ТулГУ"),
        FieldSpec("full_name", "ФИО", required=True, synonyms=("контакт",),
                  example="Петрова Анна Сергеевна"),
        FieldSpec("phone", "Телефон", F.PHONE, example="+7 900 123-45-67"),
        FieldSpec("email", "Email", F.EMAIL, synonyms=("почта",), example="petrova@tulsu.ru"),
        FieldSpec("position", "Должность", example="Проректор"),
        FieldSpec("contact_methods", "Способы связи", F.LIST, list_sep=";,",
                  example="почта; телефон"),
    ),
    parts=("contact",),
)  # fmt: skip

INTERACTIONS = KindSpec(
    key="interactions",
    label="Реестр работы с вузами",
    order=9,
    sheet_synonyms=("реестр", "заявки", "interactions", "реестр работы с вузами"),
    fields=(
        FieldSpec("university", "Название ВУЗа", F.REF, required=True, group=True,
                  ref="university", synonyms=("вуз", "инн вуза", "название вуза"),
                  example="ТулГУ"),
        FieldSpec("vendor", "Вендор", F.REF, ref="vendor", example="ПАО «Ростелеком»"),
        FieldSpec("product", "ПО", F.REF, ref="product", synonyms=("продукт", "ит-продукт"),
                  example="RT.DataLake"),
        FieldSpec("program", "ИТ-программа", F.REF, ref="program",
                  synonyms=("программа", "курс"),
                  description="пусто - единственная программа продукта"),
        FieldSpec("manager", "ФИО Менеджера", F.REF, group=True, ref="manager",
                  synonyms=("кам", "ответственный", "менеджер"),
                  description="«Фамилия Имя Отчество», «Фамилия И.О.» или email",
                  example="Петров Дмитрий Кириллович"),
        FieldSpec("contract_number", "Номер договора", group=True, contract=True,
                  example="Д-2026/15"),
        FieldSpec("contract_signed_at", "Дата договора", F.DATE, group=True, contract=True,
                  synonyms=("дата подписания договора",), example="10.09.2026"),
        FieldSpec("contract_valid_until", "Договор действует до", F.DATE, group=True,
                  contract=True, example="10.09.2028"),
        FieldSpec("license_signed_at", "Подписание лицензии", F.DATE, contract=True,
                  synonyms=("дата лицензии",), example="12.09.2026"),
        FieldSpec("license_term", "Срок действия лицензии (год)", contract=True,
                  synonyms=("срок лицензии",),
                  description="1-50 - число лет; четыре цифры - год окончания",
                  example="2"),
        FieldSpec("transfer_status", "Статус по передаче", F.ENUM, contract=True,
                  enum_values=_TRANSFER, synonyms=("статус по передачи", "статус передачи"),
                  example="передано"),
        FieldSpec("contacts", "Ответственные от ВУЗа", group=True,
                  synonyms=("ответственные от вуза", "контакты вуза"),
                  description="люди через «;»: ФИО, телефон, email",
                  example="Петрова А. С., +7 900 123-45-67, petrova@tulsu.ru"),
        FieldSpec("comment", "Комментарий", synonyms=("примечание",)),
        FieldSpec("stage", "Шаг", F.REF, ref="stage", synonyms=("этап",),
                  description="название или номер шага маршрута", example="2"),
        FieldSpec("status", "Статус", F.ENUM, group=True, enum_values=_STATUS,
                  description="сверяется с тем, что получится; «на паузе» - пауза заявки",
                  example="в работе"),
        FieldSpec("stage_since", "На шаге с", F.DATE, example="01.03.2026"),
        FieldSpec("close_reason", "Причина закрытия", F.REF, ref="close_reason",
                  description="отказ по ветке - причина уровня «ветка»"),
        FieldSpec("teachers_trained", "Обучено преподавателей", F.INT, ignored=True,
                  description="данные LMS - после интеграции (В17)"),
        FieldSpec("pause_until", "Пауза до", F.DATE, group=True, extra=True),
        FieldSpec("branch_paused", "Ветка на паузе", F.BOOL, extra=True),
        FieldSpec("branch_pause_until", "Пауза ветки до", F.DATE, extra=True),
        FieldSpec("agreement_open", "Идёт допсоглашение", F.BOOL, group=True, extra=True),
        FieldSpec("agreement_since", "Допсоглашение с", F.DATE, group=True, extra=True),
        FieldSpec("contract_extended_until", "Договор продлён до", F.DATE, group=True,
                  contract=True, extra=True),
        FieldSpec("contract_extended_at", "Дата продления договора", F.DATE, group=True,
                  contract=True, extra=True),
        FieldSpec("license_extended_until", "Лицензия продлена до", F.DATE, contract=True,
                  extra=True),
        FieldSpec("license_extended_at", "Дата продления лицензии", F.DATE, contract=True,
                  extra=True),
    ),
    parts=("interaction",),
)  # fmt: skip

KINDS: dict[str, KindSpec] = {
    k.key: k
    for k in (
        DIRECTIONS,
        VENDORS,
        PRODUCTS,
        PROGRAMS,
        SPECIALTIES,
        UNIVERSITIES,
        UNIVERSITY_SPECIALTIES,
        CONTACTS,
        INTERACTIONS,
    )
}
REGISTRY = INTERACTIONS.key

# лишние ПДн (Q16): вырезаются при разборе, не сохраняются
PERSONAL_HEADERS = frozenset(
    {
        "паспорт",
        "серия паспорта",
        "номер паспорта",
        "паспортные данные",
        "снилс",
        "дата рождения",
        "адрес регистрации",
        "адрес проживания",
        "инн физлица",
        "инн физического лица",
    }
)


class Level(StrEnum):
    ERROR = "E"
    CONFLICT = "C"
    WARNING = "W"
    INFO = "I"


# коды замечаний строк и листов (§22.2): уровень и английский текст для лога
ISSUES: dict[str, tuple[Level, str]] = {
    "IMP-003": (Level.ERROR, "manager name is not in an accepted form"),
    "IMP-004": (Level.CONFLICT, "manager not found"),
    "IMP-005": (Level.CONFLICT, "several managers match"),
    "IMP-006": (Level.ERROR, "university not found"),
    "IMP-017": (Level.ERROR, "registry sheet needs a published workflow"),
    "IMP-101": (Level.INFO, "sheet kind is not recognized"),
    "IMP-104": (Level.ERROR, "too many registry rows"),
    "IMP-110": (Level.ERROR, "value does not fit the field"),
    "IMP-111": (Level.ERROR, "several records match"),
    "IMP-120": (Level.WARNING, "phone is not recognized, kept as is"),
    "IMP-121": (Level.WARNING, "product belongs to another vendor"),
    "IMP-122": (Level.WARNING, "product not found, add it on the products sheet"),
    "IMP-123": (Level.WARNING, "region matched by a similar name"),
    "IMP-130": (Level.WARNING, "contract data differs and is kept"),
    "IMP-140": (Level.INFO, "merged into an earlier row"),
    "IMP-141": (Level.ERROR, "contradicts an earlier row"),
    "IMP-142": (Level.ERROR, "source row was not applied"),
    "IMP-150": (Level.ERROR, "group field differs between rows"),
    "IMP-151": (Level.ERROR, "program is not given and not derived"),
    "IMP-152": (Level.ERROR, "product is not a product of the program"),
    "IMP-153": (Level.WARNING, "vendor or product will be created"),
    "IMP-154": (Level.ERROR, "program and product repeat in the group"),
    "IMP-155": (Level.ERROR, "stage is a side stage"),
    "IMP-156": (Level.ERROR, "main and branch stages are mixed"),
    "IMP-157": (Level.ERROR, "different main stages in the group"),
    "IMP-158": (Level.ERROR, "terminal main stage, close branches instead"),
    "IMP-159": (Level.ERROR, "a contract puts the interaction on the signing stage"),
    "IMP-160": (Level.ERROR, "no default stage for a transferred branch"),
    "IMP-161": (Level.WARNING, "transferred branch on the branch start"),
    "IMP-162": (Level.WARNING, "no manager, the interaction stays a draft"),
    "IMP-163": (Level.ERROR, "refused branch needs a close reason"),
    "IMP-164": (Level.ERROR, "workflow has no stage to close the interaction"),
    "IMP-165": (Level.WARNING, "status differs from the result"),
    "IMP-166": (Level.ERROR, "pause needs a stage"),
    "IMP-167": (Level.ERROR, "person is not a manager"),
    "IMP-168": (Level.ERROR, "manager cannot take work"),
    "IMP-169": (Level.ERROR, "manager is required"),
    "IMP-170": (Level.ERROR, "pause term is not allowed"),
    "IMP-171": (Level.ERROR, "branch pause is not allowed"),
    "IMP-172": (Level.ERROR, "supplementary agreement cannot be opened"),
    "IMP-173": (Level.ERROR, "contract extension is not allowed"),
    "IMP-174": (Level.ERROR, "license extension is not allowed"),
    "IMP-175": (Level.WARNING, "manager limit is exceeded"),
    "IMP-176": (Level.ERROR, "contract or license dates are not allowed"),
    "IMP-177": (Level.WARNING, "stage date is before signing, signing date taken"),
    "IMP-178": (Level.WARNING, "contract numbers differ, the file one taken"),
    "IMP-179": (Level.ERROR, "no stage to attach the file to"),
    "IMP-180": (Level.CONFLICT, "open interaction of another manager"),
    "IMP-181": (Level.CONFLICT, "open interaction with other branches"),
    "IMP-182": (Level.CONFLICT, "open interaction on another stage"),
    "IMP-183": (Level.WARNING, "group is skipped, files and contacts are not applied"),
    "IMP-184": (Level.ERROR, "stage is not reached yet"),
    "IMP-185": (Level.WARNING, "only the first contact goes to the step field"),
    "IMP-186": (Level.CONFLICT, "interaction to replace changed after the decision"),
    "IMP-187": (
        Level.CONFLICT,
        "no closing stage for the workflow of the replaced one",
    ),
    "IMP-188": (Level.WARNING, "extension date moved to the branch stage date"),
    "IMP-189": (Level.ERROR, "another row of the group has an error"),
    "IMP-190": (Level.ERROR, "changed since the preview"),
    "IMP-191": (Level.ERROR, "a system rule refused at apply"),
    "IMP-192": (Level.ERROR, "database conflict at apply"),
    "IMP-193": (Level.INFO, "apply was stopped"),
    "IMP-194": (Level.ERROR, "identity provider is unavailable"),
}


def issue(code: str, field_key: str | None = None, **params) -> dict:
    """замечание строки: только код, поле и параметры без значений ячеек (M6)"""
    level, _ = ISSUES[code]
    return {"field": field_key, "level": level.value, "code": code, "params": params}
