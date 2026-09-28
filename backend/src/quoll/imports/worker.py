"""Применение партии в фоне: аренда, единицы, завершение (import-design §17).

Единица - строка каталога (группа реестра - следующим коммитом). Итог
единицы пишется в её транзакции (M12), каждая транзакция начинается с
lock_row(ImportBatch) и сверки lease_version (M11)
"""

import logging
import os
import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from quoll.auth.audit import record
from quoll.auth.audit_models import AuditEventType, TargetType
from quoll.config import settings
from quoll.core.exceptions import AppException
from quoll.core.locking import lock_row
from quoll.core.system_defaults import SystemDefaults
from quoll.imports import analysis, apply
from quoll.imports.models import (
    BatchStatus,
    ImportBatch,
    ImportRow,
    ImportSheet,
    RowResult,
    RowStatus,
)
from quoll.imports.spec import KINDS, REGISTRY

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
# строки, которые не применяются: итог сразу
_NOT_APPLIED = (RowStatus.EXCLUDED, RowStatus.ERROR, RowStatus.SAME)


class Stopped(Exception):
    """аренду перехватили или партию остановили - исполнитель выходит"""


def _now() -> datetime:
    return datetime.now(UTC)


def _lease() -> datetime:
    return _now() + timedelta(seconds=SystemDefaults.IMPORT_LEASE_SECONDS)


async def claim(maker: async_sessionmaker) -> tuple[int, int] | None:
    """взять партию в работу (§17.2): (id, lease_version) или None"""
    now = _now()
    async with maker() as db, db.begin():
        batch = await db.scalar(
            select(ImportBatch)
            .where(
                ImportBatch.status == BatchStatus.APPLYING,
                (ImportBatch.lease_until.is_(None)) | (ImportBatch.lease_until < now),
                (ImportBatch.next_attempt_at.is_(None))
                | (ImportBatch.next_attempt_at <= now),
            )
            .order_by(ImportBatch.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if batch is None:
            return None
        batch.lease_version += 1
        batch.worker_id = WORKER_ID
        batch.lease_until = _lease()
        batch.next_attempt_at = None
        return batch.id, batch.lease_version


async def tick(maker: async_sessionmaker) -> int:
    claimed = await claim(maker)
    if claimed is None:
        return 0
    await run(maker, *claimed)
    return 1


async def _locked(db: AsyncSession, batch_id: int, version: int) -> ImportBatch:
    batch = await lock_row(db, ImportBatch, batch_id)
    if (
        batch is None
        or batch.status != BatchStatus.APPLYING
        or batch.lease_version != version
    ):
        raise Stopped
    batch.lease_until = _lease()
    return batch


def _progress(batch: ImportBatch, done: int) -> None:
    summary = dict(batch.summary or {})
    progress = dict(summary.get("progress") or {"done": 0, "total": 0})
    progress["done"] = progress.get("done", 0) + done
    summary["progress"] = progress
    batch.summary = summary


def _skip_code(row: ImportRow) -> str | None:
    if row.status == RowStatus.ERROR:
        return next((i["code"] for i in row.issues if i["level"] == "E"), None)
    return None


async def run(maker: async_sessionmaker, batch_id: int, version: int) -> None:
    """единицы по порядку видов; потерявший аренду выходит без записи (M11)"""
    try:
        await _skip_rows(maker, batch_id, version)
        for kind in sorted(KINDS.values(), key=lambda k: k.order):
            if kind.key == REGISTRY:
                continue
            ids = await _pending(maker, batch_id, kind.key)
            if not ids:
                continue
            # снимок вида - заново: предыдущие виды уже в базе (M3)
            async with maker() as db:
                batch = await db.get(ImportBatch, batch_id)
                an = await analysis.analyze(db, batch, upto=kind.key)
            fresh = {c.row.id: c for c in an.of_kind(kind.key)}
            for row_id in ids:
                await _unit(maker, batch_id, version, row_id, fresh.get(row_id))
        async with maker() as db, db.begin():
            batch = await lock_row(db, ImportBatch, batch_id)
            await finish(db, batch, version)
    except Stopped:
        logger.info(f"Import {batch_id}: worker stopped")


async def _skip_rows(maker: async_sessionmaker, batch_id: int, version: int) -> None:
    """строки, которым нечего применять, - итог одной транзакцией"""
    async with maker() as db, db.begin():
        batch = await _locked(db, batch_id, version)
        rows = list(
            await db.scalars(
                select(ImportRow).where(
                    ImportRow.batch_id == batch_id,
                    ImportRow.result.is_(None),
                    ImportRow.status.in_(_NOT_APPLIED),
                )
            )
        )
        for row in rows:
            row.result = RowResult.SKIPPED
            row.result_code = _skip_code(row)
        if rows:
            _progress(batch, len(rows))


async def _pending(maker: async_sessionmaker, batch_id: int, kind: str) -> list[int]:
    async with maker() as db:
        return list(
            await db.scalars(
                select(ImportRow.id)
                .join(ImportSheet, ImportSheet.id == ImportRow.sheet_id)
                .where(
                    ImportRow.batch_id == batch_id,
                    ImportRow.result.is_(None),
                    ImportSheet.kind == kind,
                )
                .order_by(ImportSheet.number, ImportRow.number)
            )
        )


async def _unit(
    maker: async_sessionmaker,
    batch_id: int,
    version: int,
    row_id: int,
    ctx: analysis.RowCtx | None,
) -> None:
    """одна строка каталога (§17.3)"""
    async with maker() as db:
        try:
            async with db.begin():
                batch = await _locked(db, batch_id, version)
                row = await db.get(ImportRow, row_id, populate_existing=True)
                if row is None or row.result is not None:
                    return
                if ctx is None or ctx.status in _NOT_APPLIED:
                    row.result, row.result_code = RowResult.SKIPPED, None
                else:
                    await _apply(db, row, ctx)
                _progress(batch, 1)
        except Stopped:
            raise
        except Exception:
            logger.exception(f"Import {batch_id}: row {row_id} failed")
            await _attempt(maker, batch_id, version, row_id)


async def _apply(db: AsyncSession, row: ImportRow, ctx: analysis.RowCtx) -> None:
    try:
        async with db.begin_nested():
            # перепроверка: стала ошибкой или конфликтом - не применяется (M3)
            if ctx.status in (RowStatus.ERROR, RowStatus.CONFLICT):
                raise AppException(409, "Changed since the preview", "IMP-190")
            ctx.row = row
            ids = await apply.apply_row(db, ctx)
        row.result, row.result_ids, row.result_code = RowResult.APPLIED, ids, None
    except AppException as err:
        row.result = RowResult.FAILED
        row.result_code = (
            err.code if err.code and err.code.startswith("IMP-") else "IMP-191"
        )
    except DBAPIError:
        # IntegrityError - его подкласс: запись создали параллельно
        row.result, row.result_code = RowResult.FAILED, "IMP-192"


async def _attempt(
    maker: async_sessionmaker, batch_id: int, version: int, row_id: int
) -> None:
    """неожиданный сбой единицы: попытка засчитывается; предел - FAILED,
    иначе аренда отпускается и единица повторится (M16)"""
    try:
        async with maker() as db, db.begin():
            batch = await _locked(db, batch_id, version)
            row = await db.get(ImportRow, row_id, populate_existing=True)
            row.attempts += 1
            if row.attempts >= SystemDefaults.IMPORT_UNIT_ATTEMPTS:
                row.result, row.result_code = RowResult.FAILED, "IMP-191"
                _progress(batch, 1)
                return
            batch.lease_until = None
            batch.worker_id = None
            batch.next_attempt_at = _now() + timedelta(
                seconds=SystemDefaults.IMPORT_UNIT_RETRY_SECONDS
            )
    except Stopped:
        raise
    except Exception:
        # база недоступна - аренда истечёт, такт повторит
        logger.exception(f"Import {batch_id}: cannot count the attempt")
    raise Stopped


async def finish(
    db: AsyncSession,
    batch: ImportBatch | None,
    version: int | None,
    *,
    stopped: bool = False,
    actor_id: str | None = None,
) -> bool:
    """общее завершение исполнителя и остановки (§17.3); партия уже под
    lock_row. Вернёт, завершена ли партия этим вызовом"""
    if batch is None or batch.status != BatchStatus.APPLYING:
        return False
    if stopped:
        # ограждает исполнителя: его следующая транзакция увидит чужую версию
        batch.lease_version += 1
        await db.execute(
            update(ImportRow)
            .where(ImportRow.batch_id == batch.id, ImportRow.result.is_(None))
            .values(result=RowResult.SKIPPED, result_code="IMP-193")
        )
    else:
        if batch.lease_version != version:
            return False
        left = await db.scalar(
            select(func.count())
            .select_from(ImportRow)
            .where(ImportRow.batch_id == batch.id, ImportRow.result.is_(None))
        )
        if left:
            return False
    now = _now()
    batch.status = BatchStatus.APPLIED
    batch.applied_at = now
    batch.expires_at = now + timedelta(days=settings.import_applied_ttl_days)
    batch.lease_until = batch.worker_id = batch.next_attempt_at = None
    results = await _results(db, batch.id)
    summary = dict(batch.summary or {})
    summary["results"] = results
    progress = dict(summary.get("progress") or {})
    progress["done"] = progress.get("total", progress.get("done", 0))
    summary["progress"] = progress
    batch.summary = summary
    counts: dict[str, int] = {}
    for by_result in results.values():
        for result, count in by_result.items():
            counts[result] = counts.get(result, 0) + count
    record(
        db,
        actor_id=actor_id if stopped else batch.created_by,
        event_type=(
            AuditEventType.IMPORT_STOPPED if stopped else AuditEventType.IMPORT_APPLIED
        ),
        target_type=TargetType.IMPORT_BATCH,
        target_id=batch.id,
        new_value={"batch_id": batch.id, **counts},
    )
    return True


async def _results(db: AsyncSession, batch_id: int) -> dict[str, dict[str, int]]:
    """итоги по видам и результатам - запросом, не из памяти исполнителя"""
    rows = await db.execute(
        select(ImportSheet.kind, ImportRow.result, func.count())
        .join(ImportSheet, ImportSheet.id == ImportRow.sheet_id)
        .where(ImportRow.batch_id == batch_id)
        .group_by(ImportSheet.kind, ImportRow.result)
    )
    out: dict[str, dict[str, int]] = {}
    for kind, result, count in rows:
        out.setdefault(kind or "skipped", {})[result or "NONE"] = count
    return out
