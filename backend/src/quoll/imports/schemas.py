"""Запросы и ответы /api/v1/imports (import-design §21)"""

from datetime import date, datetime
from typing import Any

from pydantic import ConfigDict, Field

from quoll.core.schemas import AppBaseModel


class _Write(AppBaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldRead(AppBaseModel):
    key: str
    label: str
    type: str
    required: bool
    contract: bool
    group: bool
    extra: bool
    ignored: bool
    description: str
    example: str
    synonyms: list[str]
    enum_values: dict[str, str]


class KindRead(AppBaseModel):
    key: str
    label: str
    order: int
    fields: list[FieldRead]


class HeaderRead(AppBaseModel):
    index: int
    title: str
    field: str | None
    note: str | None


class SheetRead(AppBaseModel):
    number: int
    name: str
    kind: str | None
    header_row: int
    headers: list[HeaderRead]
    counts: dict[str, int]
    issues: list[dict[str, Any]]


class BatchBrief(AppBaseModel):
    id: int
    filename: str
    file_format: str
    status: str
    version: int
    created_by: str | None
    created_at: datetime
    applied_at: datetime | None
    expires_at: datetime


class BatchRead(BatchBrief):
    workflow_id: int | None
    replace_stages: dict[str, int]
    summary: dict[str, Any]
    next_attempt_at: datetime | None
    sheets: list[SheetRead]


class BatchPatch(_Write):
    workflow_id: int | None = None
    replace_stages: dict[str, int] | None = None


class SheetPatch(_Write):
    # null - лист пропускается
    kind: str | None = None
    # {"<индекс колонки>": "<ключ поля>" | null}
    mapping: dict[str, str | None] | None = None


class ImportFileRead(AppBaseModel):
    id: int
    row_id: int
    attachment_id: int | None
    document_id: int | None
    filename: str | None
    kind: str
    stage_id: int | None
    title: str | None
    description: str | None
    contract_number: str | None
    contract_signed_at: date | None
    contract_valid_until: date | None
    created_at: datetime


class RowRead(AppBaseModel):
    id: int
    sheet: int
    number: int
    status: str
    # {поле: ячейка} - только сопоставленные колонки
    source: dict[str, str]
    edits: dict[str, Any]
    values: dict[str, Any]
    labels: dict[str, str]
    diff: dict[str, list]
    issues: list[dict[str, Any]]
    targets: list[dict[str, Any]]
    group_key: str | None
    decision: str | None
    manager_choice: str | None
    contacts_target: str | None
    excluded: bool
    files: list[ImportFileRead]
    result: str | None
    result_code: str | None


class RowsRead(AppBaseModel):
    total: int
    rows: list[RowRead]


class RowPatch(_Write):
    # строка - как ячейка, {"id": n} - запись справочника, null - снять правку
    edits: dict[str, str | dict[str, int] | None] | None = None
    excluded: bool | None = None


class RowUpdateRead(AppBaseModel):
    rows: list[RowRead]
    summary: dict[str, Any]
    version: int


class ApplyRequest(_Write):
    version: int = Field(ge=0)
