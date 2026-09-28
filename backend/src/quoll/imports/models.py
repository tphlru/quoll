"""Партия импорта: листы, строки, файлы строк (import-design §12)"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quoll.core.pii import EncryptedJSON, EncryptedString
from quoll.db import Base, created_at_dt, updated_at_dt


class BatchStatus(StrEnum):
    DRAFT = "DRAFT"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"


class FileFormat(StrEnum):
    XLSX = "XLSX"
    XLS = "XLS"
    CSV = "CSV"
    JSON = "JSON"


class RowStatus(StrEnum):
    NEW = "NEW"
    UPDATE = "UPDATE"
    SAME = "SAME"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"
    EXCLUDED = "EXCLUDED"


class RowResult(StrEnum):
    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class Decision(StrEnum):
    SKIP = "SKIP"
    REPLACE = "REPLACE"


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "file_format IN ('XLSX', 'XLS', 'CSV', 'JSON')",
            name="chk_import_batch_format",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'APPLYING', 'APPLIED')",
            name="chk_import_batch_status",
        ),
        # аренда только у применяемой - иначе взятие и CAS спорили бы
        CheckConstraint(
            "status = 'APPLYING' OR (lease_until IS NULL AND worker_id IS NULL)",
            name="chk_import_batch_lease",
        ),
        Index("ix_import_batches_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # имя файла бывает с ФИО
    filename: Mapped[str] = mapped_column(EncryptedString)
    file_format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(
        String(12), default=BatchStatus.DRAFT, server_default=BatchStatus.DRAFT
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=True
    )
    # {workflow_id: stage_id} - куда закрывать заменяемые заявки (В7)
    replace_stages: Mapped[dict[str, int]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idp_retries: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    created_at: Mapped[created_at_dt]
    updated_at: Mapped[updated_at_dt]
    apply_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ImportSheet(Base):
    __tablename__ = "import_sheets"
    __table_args__ = (UniqueConstraint("batch_id", "number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    # имя листа и заголовки - любой текст из файла
    name: Mapped[str] = mapped_column(EncryptedJSON)
    kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    header_row: Mapped[int] = mapped_column(Integer)
    headers: Mapped[list[str]] = mapped_column(EncryptedJSON)
    # {"<индекс колонки>": "<ключ поля>"} - только индексы и ключи
    mapping: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    notes: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("sheet_id", "number"),
        Index("ix_import_rows_batch_status", "batch_id", "status"),
        Index("ix_import_rows_batch_group", "batch_id", "group_key"),
        Index("ix_import_rows_batch_result", "batch_id", "result"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE")
    )
    sheet_id: Mapped[int] = mapped_column(
        ForeignKey("import_sheets.id", ondelete="CASCADE")
    )
    number: Mapped[int] = mapped_column(Integer)
    # всё из ячеек - одним шифртекстом: raw, edits, values, labels, diff (Т1)
    data: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    status: Mapped[str] = mapped_column(String(12))
    # только коды и параметры без значений ячеек (M6)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    targets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    group_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    replace_target: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    manager_choice: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contacts_target: Mapped[str | None] = mapped_column(String(60), nullable=True)
    excluded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    digest: Mapped[str] = mapped_column(String(64), default="", server_default="")
    result: Mapped[str | None] = mapped_column(String(10), nullable=True)
    result_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ImportFile(Base):
    __tablename__ = "import_files"
    __table_args__ = (
        CheckConstraint(
            "attachment_id IS NOT NULL OR document_id IS NOT NULL",
            name="chk_import_file_target",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    row_id: Mapped[int] = mapped_column(
        ForeignKey("import_rows.id", ondelete="CASCADE"), index=True
    )
    # CASCADE, не SET NULL: иначе удаление документа нарушило бы CHECK
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("interaction_documents.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(50))
    stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    contract_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contract_signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[created_at_dt]
