from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from quoll.catalog.regions import REGIONS
from quoll.core import validators
from quoll.core.schemas import AppBaseModel


class _Write(AppBaseModel):
    model_config = ConfigDict(extra="forbid")


class _Patch(_Write):
    # явный null в NOT NULL ушёл бы в базу и вернулся 409 вместо 422
    required: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _no_nulls(self):
        for field in self.required:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class DirectionWrite(_Write):
    name: str = Field(min_length=1, max_length=255)


class DirectionPatch(_Patch):
    required = ("name",)
    name: str | None = Field(default=None, min_length=1, max_length=255)


class DocumentKindWrite(_Write):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,49}$")
    label: str = Field(min_length=1, max_length=255)


class DocumentKindPatch(_Patch):
    # код - ключ для воркфлоу и интеграций, не меняется
    required = ("label",)
    label: str | None = Field(default=None, min_length=1, max_length=255)


class DocumentKindRead(AppBaseModel):
    id: int
    code: str
    label: str
    is_system: bool


class CloseReasonWrite(_Write):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,49}$")
    label: str = Field(min_length=1, max_length=255)
    level: Literal["INTERACTION_BEFORE_SIGNING", "INTERACTION_AFTER_SIGNING", "BRANCH"]
    outcome: Literal["DONE", "REFUSED"]
    needs_comment: bool = False


class CloseReasonPatch(_Patch):
    required = ("label", "needs_comment")
    label: str | None = Field(default=None, min_length=1, max_length=255)
    needs_comment: bool | None = None


class CloseReasonRead(AppBaseModel):
    id: int
    code: str
    label: str
    level: str
    outcome: str
    needs_comment: bool
    is_system: bool


class DirectionRead(AppBaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime


class Ref(AppBaseModel):
    id: int
    name: str


class ProductRef(Ref):
    vendor_id: int


class ProductWrite(_Write):
    name: str = Field(min_length=1, max_length=255)
    vendor_id: int
    contact_id: int | None = None
    # у продукта минимум одно направление
    direction_ids: list[int] = Field(min_length=1)
    description: str | None = None
    url: str | None = Field(default=None, max_length=500)
    is_active: bool = True


class ProductPatch(_Patch):
    required = ("name", "vendor_id", "direction_ids", "is_active")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    vendor_id: int | None = None
    contact_id: int | None = None
    direction_ids: list[int] | None = Field(default=None, min_length=1)
    description: str | None = None
    url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class ProductRead(AppBaseModel):
    id: int
    name: str
    vendor_id: int
    contact_id: int | None
    directions: list[Ref]
    description: str | None
    url: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProgramWrite(_Write):
    name: str = Field(min_length=1, max_length=255)
    direction_id: int
    # пусто - программа продуктонезависимая
    product_ids: list[int] = Field(default_factory=list)
    description: str | None = None
    url: str | None = Field(default=None, max_length=500)
    site_course_id: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class ProgramPatch(_Patch):
    required = ("name", "direction_id", "product_ids", "is_active")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    direction_id: int | None = None
    product_ids: list[int] | None = None
    description: str | None = None
    url: str | None = Field(default=None, max_length=500)
    site_course_id: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class PriorityWrite(_Write):
    # 1 - самая востребованная; null - снять приоритет
    priority: int | None = Field(ge=1, le=1000)


class ProgramRead(AppBaseModel):
    id: int
    name: str
    direction_id: int
    products: list[ProductRef]
    description: str | None
    url: str | None
    priority: int | None
    site_course_id: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SpecialtyWrite(_Write):
    code: str = Field(pattern=r"^\d{2}\.\d{2}\.\d{2}$")
    name: str = Field(min_length=1, max_length=255)
    level: Literal["BACHELOR", "SPECIALIST", "MASTER"]
    direction_ids: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class SpecialtyPatch(_Patch):
    required = ("code", "name", "level", "direction_ids", "tags")
    code: str | None = Field(default=None, pattern=r"^\d{2}\.\d{2}\.\d{2}$")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    level: Literal["BACHELOR", "SPECIALIST", "MASTER"] | None = None
    direction_ids: list[int] | None = None
    tags: list[str] | None = None


class SpecialtyRead(AppBaseModel):
    id: int
    code: str
    name: str
    level: str
    directions: list[Ref]
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class UniversitySpecialtiesWrite(_Write):
    specialty_ids: list[int]


def _region(value: str | None) -> str | None:
    if value is not None and value not in REGIONS:
        raise ValueError("Region must be a subject of the Russian Federation")
    return value


Inn = Annotated[str, AfterValidator(validators.inn)]
Kpp = Annotated[str | None, AfterValidator(validators.kpp)]
Region = Annotated[str, AfterValidator(_region)]


class UniversityWrite(_Write):
    full_name: str = Field(min_length=1)
    short_name: str = Field(min_length=1, max_length=255)
    inn: Inn
    kpp: Kpp = None
    site: str | None = Field(default=None, max_length=255)
    region: Region
    city: str = Field(min_length=1, max_length=255)


class UniversityPatch(_Patch):
    required = ("full_name", "short_name", "inn", "region", "city")
    full_name: str | None = Field(default=None, min_length=1)
    short_name: str | None = Field(default=None, min_length=1, max_length=255)
    inn: Annotated[str | None, AfterValidator(lambda v: v and validators.inn(v))] = None
    kpp: Kpp = None
    site: str | None = Field(default=None, max_length=255)
    region: Annotated[str | None, AfterValidator(_region)] = None
    city: str | None = Field(default=None, min_length=1, max_length=255)


class UniversityRead(AppBaseModel):
    id: int
    full_name: str
    short_name: str
    inn: str
    kpp: str | None
    site: str | None
    region: str
    city: str
    created_at: datetime
    updated_at: datetime


class VendorWrite(_Write):
    name: str = Field(min_length=1, max_length=255)
    site: str | None = Field(default=None, max_length=255)
    kind: str | None = Field(default=None, max_length=255)


class VendorPatch(_Patch):
    required = ("name",)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    site: str | None = Field(default=None, max_length=255)
    kind: str | None = Field(default=None, max_length=255)


class VendorRead(AppBaseModel):
    id: int
    name: str
    site: str | None
    kind: str | None
    created_at: datetime
    updated_at: datetime


class ContactWrite(_Write):
    # ровно одно: вуз или вендор
    university_id: int | None = None
    vendor_id: int | None = None
    full_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, max_length=255)
    contact_methods: list[str] = Field(default_factory=list)
    is_actual: bool = True

    @model_validator(mode="after")
    def _one_owner(self):
        if (self.university_id is None) == (self.vendor_id is None):
            raise ValueError("Contact belongs to exactly one of university or vendor")
        return self


class ContactPatch(_Patch):
    required = ("full_name", "contact_methods", "is_actual")
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, max_length=255)
    contact_methods: list[str] | None = None
    is_actual: bool | None = None


class ContactRead(AppBaseModel):
    id: int
    university_id: int | None
    vendor_id: int | None
    full_name: str
    phone: str | None
    email: str | None
    position: str | None
    contact_methods: list[str]
    is_actual: bool
    created_at: datetime
    updated_at: datetime


class SpecialtyRef(AppBaseModel):
    code: str
    name: str


class ProgramSuggestion(AppBaseModel):
    """подсказка шага 0 (М 3.21): ни на что не влияет и не сохраняется"""

    program: "ProgramRead"
    matched: list[SpecialtyRef]
