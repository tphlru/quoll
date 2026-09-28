"""Операции над партией: загрузка, правки, файлы, запуск, отмена
(import-design §13, §17.1, §21). Все правки - под lock_row(ImportBatch) (M8)"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from quoll.attachments.models import Attachment
from quoll.attachments.s3 import S3StorageService
from quoll.attachments.service import AttachmentService
from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.catalog.models import DocumentKind
from quoll.config import settings
from quoll.core.base_repository import BaseRepository
from quoll.core.exceptions import IdNotExistsException
from quoll.core.locking import lock_row
from quoll.imports import analysis, mapping, parsing, worker
from quoll.imports.errors import import_error, wrong_status
from quoll.imports.models import (
    BatchStatus,
    ImportBatch,
    ImportFile,
    ImportRow,
    ImportSheet,
    RowStatus,
)
from quoll.imports.schemas import BatchPatch, RowPatch, SheetPatch
from quoll.imports.spec import KINDS, REGISTRY
from quoll.workflows.models import Stage, Workflow

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
ROWS_LIMIT = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _touch(batch: ImportBatch) -> None:
    """черновик живёт сутки с последней правки (§13.3)"""
    batch.expires_at = _now() + timedelta(hours=settings.import_draft_ttl_hours)


async def _read_limited(file: UploadFile) -> bytes:
    """потоком: на пределе чтение обрывается (413 IMP-008)"""
    limit = settings.import_max_size_mb * 1024 * 1024
    chunks, size = [], 0
    while chunk := await file.read(_CHUNK):
        size += len(chunk)
        if size > limit:
            raise import_error(
                413,
                "IMP-008",
                "File is too large",
                limit_mb=settings.import_max_size_mb,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _workflow(session: AsyncSession, workflow_id: int | None) -> int | None:
    """маршрут реестра: заданный - опубликованный; не задан - единственный
    опубликованный или никакого"""
    if workflow_id is None:
        published = list(
            await session.scalars(
                select(Workflow.id).where(Workflow.is_published.is_(True)).limit(2)
            )
        )
        return published[0] if len(published) == 1 else None
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise IdNotExistsException(Workflow.__name__)
    if not workflow.is_published:
        raise import_error(
            422, "IMP-017", "Workflow is not published", workflow_id=workflow_id
        )
    return workflow_id


async def upload(
    session: AsyncSession, admin_id: str, file: UploadFile, workflow_id: int | None
) -> ImportBatch:
    """разбор файла в партию; сам файл не хранится (§13.2)"""
    fmt = parsing.file_format(file.filename)
    content = await _read_limited(file)
    sheets = parsing.read(content, fmt, file.filename or "")
    batch = ImportBatch(
        created_by=admin_id,
        filename=Path((file.filename or "").replace("\\", "/")).name,
        file_format=fmt,
        status=BatchStatus.DRAFT,
        workflow_id=await _workflow(session, workflow_id),
        replace_stages={},
        summary={},
        version=0,
    )
    _touch(batch)
    session.add(batch)
    await session.flush()
    single = len(sheets) == 1
    total = 0
    for number, parsed in enumerate(sheets, start=1):
        cells = [c for _, c in parsed.rows]
        found = mapping.detect(parsed.name, parsed.headers, cells, single)
        personal = mapping.personal_columns(parsed.headers)
        sheet = ImportSheet(
            batch_id=batch.id,
            number=number,
            name=parsed.name,
            kind=found.kind,
            header_row=parsed.header_row,
            headers=parsed.headers,
            mapping=found.mapping,
            notes=found.notes,
            issues=[],
        )
        session.add(sheet)
        await session.flush()
        for row_number, raw in parsed.rows:
            # лишние ПДн вырезаются сразу и не сохраняются (Q16)
            raw = ["" if i in personal else v for i, v in enumerate(raw)]
            session.add(
                ImportRow(
                    batch_id=batch.id,
                    sheet_id=sheet.id,
                    number=row_number,
                    data={"raw": raw, "edits": {}},
                    status=RowStatus.NEW,
                    issues=[],
                    targets=[],
                )
            )
        total += len(parsed.rows)
    await session.flush()
    await analysis.run(session, batch)
    record(
        session,
        actor_id=admin_id,
        event_type=AuditEventType.IMPORT_UPLOADED,
        target_type=TargetType.IMPORT_BATCH,
        target_id=batch.id,
        new_value={"batch_id": batch.id, "format": fmt, "rows": total},
    )
    await session.flush()
    return batch


async def get_batch(session: AsyncSession, batch_id: int) -> ImportBatch:
    batch = await session.get(ImportBatch, batch_id)
    if batch is None:
        raise IdNotExistsException(ImportBatch.__name__)
    return batch


async def _draft(session: AsyncSession, batch_id: int) -> ImportBatch:
    batch = await lock_row(session, ImportBatch, batch_id)
    if batch is None:
        raise IdNotExistsException(ImportBatch.__name__)
    if batch.status != BatchStatus.DRAFT:
        raise wrong_status(batch.status)
    _touch(batch)
    return batch


async def listing(
    session: AsyncSession, status: str | None, limit: int, offset: int
) -> list[ImportBatch]:
    stmt = (
        select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit).offset(offset)
    )
    if status is not None:
        stmt = stmt.where(ImportBatch.status == status)
    return list(await session.scalars(stmt))


async def sheets(session: AsyncSession, batch_id: int) -> list[ImportSheet]:
    return list(
        await session.scalars(
            select(ImportSheet)
            .where(ImportSheet.batch_id == batch_id)
            .order_by(ImportSheet.number)
        )
    )


async def read_batch(session: AsyncSession, batch: ImportBatch) -> dict:
    counts = (batch.summary or {}).get("sheets", {})
    out = []
    for sheet in await sheets(session, batch.id):
        out.append(
            {
                "number": sheet.number,
                "name": sheet.name,
                "kind": sheet.kind,
                "header_row": sheet.header_row,
                "headers": [
                    {
                        "index": i,
                        "title": title,
                        "field": sheet.mapping.get(str(i)),
                        "note": sheet.notes.get(str(i)),
                    }
                    for i, title in enumerate(sheet.headers)
                ],
                "counts": counts.get(str(sheet.number), {}),
                "issues": sheet.issues,
            }
        )
    return {
        **{c: getattr(batch, c) for c in _BATCH_FIELDS},
        "sheets": out,
    }


_BATCH_FIELDS = (
    "id",
    "filename",
    "file_format",
    "status",
    "version",
    "created_by",
    "created_at",
    "applied_at",
    "expires_at",
    "workflow_id",
    "replace_stages",
    "summary",
    "next_attempt_at",
)


async def patch_batch(
    session: AsyncSession, batch_id: int, body: BatchPatch
) -> ImportBatch:
    batch = await _draft(session, batch_id)
    if "workflow_id" in body.model_fields_set:
        batch.workflow_id = await _workflow(session, body.workflow_id)
    if body.replace_stages is not None:
        batch.replace_stages = await _replace_stages(session, body.replace_stages)
    await analysis.run(session, batch)
    return batch


async def _replace_stages(
    session: AsyncSession, stages: dict[str, int]
) -> dict[str, int]:
    """стадия закрытия заменяемых: терминальная основная этого маршрута, не
    в архиве (В7)"""
    for workflow_id, stage_id in stages.items():
        stage = await session.get(Stage, stage_id)
        if (
            stage is None
            or str(stage.workflow_id) != workflow_id
            or not stage.is_terminal
            or stage.is_branch_stage
            or stage.archived_at is not None
        ):
            raise import_error(
                422,
                "IMP-014",
                "Stage cannot close replaced interactions",
                workflow_id=int(workflow_id) if workflow_id.isdigit() else None,
                stage_id=stage_id,
            )
    return dict(stages)


async def patch_sheet(
    session: AsyncSession, batch_id: int, number: int, body: SheetPatch
) -> ImportBatch:
    """смена вида пересобирает сопоставление; правка колонок - поверх"""
    batch = await _draft(session, batch_id)
    sheet = await session.scalar(
        select(ImportSheet).where(
            ImportSheet.batch_id == batch_id, ImportSheet.number == number
        )
    )
    if sheet is None:
        raise IdNotExistsException(ImportSheet.__name__)
    if "kind" in body.model_fields_set and body.kind != sheet.kind:
        if body.kind is not None and body.kind not in KINDS:
            raise import_error(422, "IMP-013", "Unknown kind or field")
        sheet.kind = body.kind
        if body.kind is None:
            sheet.mapping = {}
            sheet.notes = {
                str(i): "personal_data" for i in mapping.personal_columns(sheet.headers)
            }
        else:
            rows = await session.scalars(
                select(ImportRow)
                .where(ImportRow.sheet_id == sheet.id)
                .limit(mapping.SAMPLE_ROWS)
            )
            found = mapping.columns(
                KINDS[body.kind], sheet.headers, [r.data["raw"] for r in rows]
            )
            sheet.mapping, sheet.notes = found.mapping, found.notes
    if body.mapping:
        _remap(sheet, body.mapping)
    await analysis.run(session, batch)
    return batch


def _remap(sheet: ImportSheet, changes: dict[str, str | None]) -> None:
    if sheet.kind is None:
        raise import_error(422, "IMP-013", "Unknown kind or field")
    kind = KINDS[sheet.kind]
    personal = mapping.personal_columns(sheet.headers)
    current, notes = dict(sheet.mapping), dict(sheet.notes)
    for index, field_key in changes.items():
        if not index.isdigit() or int(index) >= len(sheet.headers):
            raise import_error(422, "IMP-013", "Unknown kind or field")
        if int(index) in personal:
            raise import_error(422, "IMP-012", "Personal data column is not imported")
        if field_key is not None and kind.field(field_key) is None:
            raise import_error(422, "IMP-013", "Unknown kind or field")
        if field_key is None:
            current.pop(index, None)
            notes[index] = "unused"
            continue
        # поле уходит с прежней колонки
        for other, key in list(current.items()):
            if key == field_key and other != index:
                del current[other]
                notes[other] = "unused"
        current[index] = field_key
        notes.pop(index, None)
    sheet.mapping, sheet.notes = current, notes


# --- строки ------------------------------------------------------------------


async def rows(
    session: AsyncSession,
    batch_id: int,
    *,
    sheet: int | None = None,
    status: list[str] | None = None,
    group_key: str | None = None,
    has_issues: bool | None = None,
    result: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    await get_batch(session, batch_id)
    stmt = select(ImportRow).where(ImportRow.batch_id == batch_id)
    if sheet is not None:
        stmt = stmt.join(ImportSheet, ImportSheet.id == ImportRow.sheet_id).where(
            ImportSheet.number == sheet
        )
    if status:
        stmt = stmt.where(ImportRow.status.in_(status))
    if group_key is not None:
        stmt = stmt.where(ImportRow.group_key == group_key)
    if has_issues is not None:
        empty = func.jsonb_array_length(ImportRow.issues) == 0
        stmt = stmt.where(~empty if has_issues else empty)
    if result is not None:
        stmt = stmt.where(ImportRow.result == result)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    page = list(
        await session.scalars(
            stmt.order_by(ImportRow.sheet_id, ImportRow.number)
            .limit(min(limit, ROWS_LIMIT))
            .offset(offset)
        )
    )
    return {"total": total, "rows": await read_rows(session, page)}


async def get_row(session: AsyncSession, batch_id: int, row_id: int) -> ImportRow:
    row = await session.get(ImportRow, row_id)
    if row is None or row.batch_id != batch_id:
        raise IdNotExistsException(ImportRow.__name__)
    return row


async def read_rows(session: AsyncSession, page: list[ImportRow]) -> list[dict]:
    if not page:
        return []
    sheet_by_id = {
        s.id: s
        for s in await session.scalars(
            select(ImportSheet).where(ImportSheet.id.in_({r.sheet_id for r in page}))
        )
    }
    files: dict[int, list[dict]] = {}
    for item, filename in await session.execute(
        select(ImportFile, Attachment.filename)
        .outerjoin(Attachment, Attachment.id == ImportFile.attachment_id)
        .where(ImportFile.row_id.in_([r.id for r in page]))
        .order_by(ImportFile.id)
    ):
        files.setdefault(item.row_id, []).append(file_view(item, filename))
    out = []
    for row in page:
        sheet = sheet_by_id[row.sheet_id]
        raw = row.data.get("raw", [])
        out.append(
            {
                "id": row.id,
                "sheet": sheet.number,
                "number": row.number,
                "status": row.status,
                "source": {
                    key: raw[int(i)]
                    for i, key in sheet.mapping.items()
                    if int(i) < len(raw)
                },
                "edits": row.data.get("edits") or {},
                "values": row.data.get("values") or {},
                "labels": row.data.get("labels") or {},
                "diff": row.data.get("diff") or {},
                "issues": row.issues,
                "targets": row.targets,
                "group_key": row.group_key,
                "decision": row.decision,
                "manager_choice": row.manager_choice,
                "contacts_target": row.contacts_target,
                "excluded": row.excluded,
                "files": files.get(row.id, []),
                "result": row.result,
                "result_code": row.result_code,
            }
        )
    return out


def file_view(item: ImportFile, filename: str | None) -> dict:
    return {
        **{
            c: getattr(item, c)
            for c in (
                "id",
                "row_id",
                "attachment_id",
                "document_id",
                "kind",
                "stage_id",
                "title",
                "description",
                "contract_number",
                "contract_signed_at",
                "contract_valid_until",
                "created_at",
            )
        },
        "filename": filename,
    }


async def patch_row(
    session: AsyncSession, batch_id: int, row_id: int, body: RowPatch
) -> dict:
    batch = await _draft(session, batch_id)
    row = await get_row(session, batch_id, row_id)
    if body.edits:
        sheet = await session.get(ImportSheet, row.sheet_id)
        kind = KINDS.get(sheet.kind or "")
        edits = dict(row.data.get("edits") or {})
        for key, value in body.edits.items():
            if kind is None or kind.field(key) is None:
                raise import_error(422, "IMP-013", "Unknown kind or field")
            if value is None:
                edits.pop(key, None)
            else:
                edits[key] = value
        row.data = {**row.data, "edits": edits}
    if body.excluded is not None:
        row.excluded = body.excluded
    await analysis.run(session, batch)
    rows_out = [row]
    if row.group_key is not None:
        rows_out = list(
            await session.scalars(
                select(ImportRow)
                .where(
                    ImportRow.batch_id == batch_id, ImportRow.group_key == row.group_key
                )
                .order_by(ImportRow.sheet_id, ImportRow.number)
            )
        )
    return {
        "rows": await read_rows(session, rows_out),
        "summary": batch.summary,
        "version": batch.version,
    }


# --- применение --------------------------------------------------------------


async def apply(session: AsyncSession, batch_id: int, version: int) -> ImportBatch:
    """свежий пересчёт; изменился итог - он сохраняется, а запуск отказывает
    с новой версией (§17.1)"""
    batch = await lock_row(session, ImportBatch, batch_id)
    if batch is None:
        raise IdNotExistsException(ImportBatch.__name__)
    if batch.status != BatchStatus.DRAFT:
        raise wrong_status(batch.status)
    if batch.version != version:
        raise _stale(batch.version)
    if await analysis.run(session, batch):
        new_version = batch.version
        # иначе get_db_session откатил бы пересчёт вместе с отказом
        await session.commit()
        raise _stale(new_version)
    conflicts = list(
        await session.scalars(
            select(ImportRow.group_key)
            .where(
                ImportRow.batch_id == batch_id, ImportRow.status == RowStatus.CONFLICT
            )
            .distinct()
        )
    )
    if conflicts:
        raise import_error(
            409,
            "IMP-011",
            "Unresolved conflicts",
            groups=sorted(g or "" for g in conflicts),
        )
    broken = [
        s.number
        for s in await sheets(session, batch_id)
        if any(i.get("level") == "E" for i in s.issues)
    ]
    if broken:
        raise import_error(
            409, "IMP-019", "Sheet errors block the import", sheets=broken
        )
    total = await session.scalar(
        select(func.count())
        .select_from(ImportRow)
        .where(ImportRow.batch_id == batch_id)
    )
    batch.status = BatchStatus.APPLYING
    batch.apply_started_at = _now()
    batch.lease_until = batch.worker_id = batch.next_attempt_at = None
    batch.summary = {**(batch.summary or {}), "progress": {"done": 0, "total": total}}
    return batch


def _stale(version: int):
    return import_error(409, "IMP-018", "Preview is outdated", version=version)


async def stop(session: AsyncSession, batch_id: int, admin_id: str) -> ImportBatch:
    """остановка: строки без итога - SKIPPED IMP-193 (§17.5)"""
    batch = await lock_row(session, ImportBatch, batch_id)
    if batch is None:
        raise IdNotExistsException(ImportBatch.__name__)
    if batch.status != BatchStatus.APPLYING:
        raise wrong_status(batch.status)
    await worker.finish(session, batch, None, stopped=True, actor_id=admin_id)
    return batch


# --- удаление ----------------------------------------------------------------


async def drop(session: AsyncSession, batch: ImportBatch) -> list[str]:
    """партия, её строки и непримененные файлы (§13.4). Вернёт ключи S3 -
    их удаляют после коммита"""
    ids = list(
        await session.scalars(
            select(ImportFile.attachment_id)
            .join(ImportRow, ImportRow.id == ImportFile.row_id)
            .where(
                ImportRow.batch_id == batch.id, ImportFile.attachment_id.is_not(None)
            )
        )
    )
    keys = (
        list(
            await session.scalars(
                select(Attachment.storage_key).where(Attachment.id.in_(ids))
            )
        )
        if ids
        else []
    )
    await session.execute(delete(ImportBatch).where(ImportBatch.id == batch.id))
    if ids:
        await session.execute(delete(Attachment).where(Attachment.id.in_(ids)))
    return keys


async def cancel(session: AsyncSession, batch_id: int, admin_id: str) -> list[str]:
    batch = await lock_row(session, ImportBatch, batch_id)
    if batch is None:
        raise IdNotExistsException(ImportBatch.__name__)
    if batch.status == BatchStatus.APPLYING:
        raise wrong_status(batch.status)
    keys = await drop(session, batch)
    record(
        session,
        actor_id=admin_id,
        event_type=AuditEventType.IMPORT_CANCELLED,
        target_type=TargetType.IMPORT_BATCH,
        target_id=batch_id,
        new_value={"batch_id": batch_id},
    )
    return keys


async def delete_keys(s3: S3StorageService | None, keys: list[str]) -> None:
    """сбой хранилища не возвращает запись: она уже удалена"""
    for key in keys:
        try:
            if s3 is not None:
                await s3.delete(key)
        except Exception:
            logger.exception(f"Cannot delete import file '{key}'")


async def cleanup(maker: async_sessionmaker, s3: S3StorageService | None) -> int:
    """истёкшие партии, кроме применяемых; каждая - своя транзакция"""
    removed = 0
    while True:
        async with maker() as db, db.begin():
            batch = await db.scalar(
                select(ImportBatch)
                .where(
                    ImportBatch.expires_at < _now(),
                    ImportBatch.status != BatchStatus.APPLYING,
                )
                .order_by(ImportBatch.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if batch is None:
                return removed
            keys = await drop(db, batch)
        await delete_keys(s3, keys)
        removed += 1


# --- файлы строк -------------------------------------------------------------


async def upload_file(
    session: AsyncSession,
    s3: S3StorageService | None,
    batch_id: int,
    row_id: int,
    file: UploadFile,
    values: dict,
) -> dict:
    """файл строки реестра: в S3 - до блокировки, как у документов;
    партия уже не черновик - объект удаляется (§21)"""
    attachment = await AttachmentService(
        BaseRepository(session, Attachment), s3
    ).upload_attachment(file)
    try:
        item = await _add_file(session, batch_id, row_id, attachment, values)
    except Exception:
        await delete_keys(s3, [attachment.storage_key])
        raise
    return file_view(item, attachment.filename)


async def _add_file(
    session: AsyncSession,
    batch_id: int,
    row_id: int,
    attachment: Attachment,
    values: dict,
) -> ImportFile:
    await _draft(session, batch_id)
    row = await get_row(session, batch_id, row_id)
    sheet = await session.get(ImportSheet, row.sheet_id)
    if row.excluded or sheet.kind != REGISTRY:
        raise import_error(409, "IMP-020", "Files go to registry rows only")
    kind = values["kind"]
    if not await session.scalar(
        select(DocumentKind.id).where(DocumentKind.code == kind)
    ):
        raise import_error(422, "IMP-016", "Unknown document kind")
    if kind == "OTHER" and not values.get("description"):
        raise import_error(422, "IMP-016", "Other document needs a description")
    contract_fields = ("contract_number", "contract_signed_at", "contract_valid_until")
    if kind != "CONTRACT" and any(values.get(f) for f in contract_fields):
        raise import_error(422, "IMP-016", "Contract details go with a contract only")
    if kind == "CONTRACT":
        group = [row.id]
        if row.group_key is not None:
            group = list(
                await session.scalars(
                    select(ImportRow.id).where(
                        ImportRow.batch_id == batch_id,
                        ImportRow.group_key == row.group_key,
                    )
                )
            )
        if await session.scalar(
            select(ImportFile.id).where(
                ImportFile.row_id.in_(group), ImportFile.kind == "CONTRACT"
            )
        ):
            raise import_error(409, "IMP-015", "Group already has a contract file")
    item = ImportFile(row_id=row.id, attachment_id=attachment.id, **values)
    session.add(item)
    await session.flush()
    await session.refresh(item)
    await analysis.run(session, await session.get(ImportBatch, batch_id))
    return item


async def delete_file(
    session: AsyncSession, batch_id: int, row_id: int, file_id: int
) -> list[str]:
    batch = await _draft(session, batch_id)
    await get_row(session, batch_id, row_id)
    item = await session.get(ImportFile, file_id)
    if item is None or item.row_id != row_id or item.attachment_id is None:
        raise IdNotExistsException(ImportFile.__name__)
    key = await session.scalar(
        select(Attachment.storage_key).where(Attachment.id == item.attachment_id)
    )
    # запись файла уходит каскадом
    await session.execute(delete(Attachment).where(Attachment.id == item.attachment_id))
    await analysis.run(session, batch)
    return [key] if key else []
