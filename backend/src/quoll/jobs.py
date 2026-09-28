"""Какие фоновые процессы поднимает приложение и с каким тактом.

отдельным модулем: процессы из разных модулей, а main.py про HTTP
"""

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from quoll.auth import role_transition
from quoll.auth.offboarding import offboard_manager, offboard_superviser
from quoll.auth.pending_actions import PendingActionType
from quoll.auth.reconciler import reconcile
from quoll.auth.session_store import SessionStore
from quoll.auth.task_queue import run_queue
from quoll.config import settings
from quoll.attachments.s3 import S3StorageService
from quoll.core.worker import Periodic
from quoll.imports import service as import_service
from quoll.imports import worker as import_worker
from quoll.interactions.pause_worker import expire_branch_pauses, expire_pauses
from quoll.interactions.watcher import watch
from quoll.reports.worker import ReportRunner

logger = logging.getLogger(__name__)


def background_jobs(
    session_maker: async_sessionmaker,
    reports: ReportRunner,
    s3: S3StorageService | None = None,
) -> list[Periodic]:
    sessions = SessionStore(session_maker)

    async def clean_sessions() -> None:
        removed = await sessions.delete_expired()
        if removed:
            logger.info(f"Removed {removed} expired sessions")

    async def expire_pauses_tick() -> None:
        resumed = await expire_pauses(session_maker)
        if resumed:
            logger.info(f"Resumed {resumed} interactions after their pause")
        branches = await expire_branch_pauses(session_maker)
        if branches:
            logger.info(f"Resumed {branches} branches after their pause")

    handlers = {
        PendingActionType.OFFBOARDING_MANAGER: offboard_manager,
        PendingActionType.OFFBOARDING_SUPERVISER: offboard_superviser,
        PendingActionType.ROLE_TRANSITION: role_transition.handle,
    }
    on_failed = {PendingActionType.ROLE_TRANSITION: role_transition.on_failed}

    async def run_org_queue() -> None:
        processed = await run_queue(session_maker, handlers, on_failed)
        if processed:
            logger.info(f"Processed {processed} org tasks")

    async def watch_tick() -> None:
        handled = await watch(session_maker)
        if handled:
            logger.info(f"Watcher handled {handled} stalls and expiring terms")

    async def report_tick() -> None:
        claimed = await reports.tick()
        if claimed:
            logger.info(f"Started {claimed} report exports")

    async def report_cleanup() -> None:
        expired = await reports.cleanup()
        if expired:
            logger.info(f"Expired {expired} report files")

    async def import_tick() -> None:
        if await import_worker.tick(session_maker):
            logger.info("Import batch processed")

    async def import_cleanup() -> None:
        removed = await import_service.cleanup(session_maker, s3)
        if removed:
            logger.info(f"Removed {removed} expired import batches")

    async def reconcile_tick() -> None:
        await reconcile(session_maker)

    return [
        Periodic("reconciler", settings.reconciler_interval_seconds, reconcile_tick),
        Periodic("org-queue", settings.org_queue_interval_seconds, run_org_queue),
        Periodic(
            "session-cleanup", settings.session_cleanup_interval_seconds, clean_sessions
        ),
        Periodic(
            "pause-expiry", settings.pause_expiry_interval_seconds, expire_pauses_tick
        ),
        Periodic("watcher", settings.watcher_interval_seconds, watch_tick),
        Periodic("report-queue", settings.report_worker_interval_seconds, report_tick),
        Periodic(
            "report-cleanup", settings.report_cleanup_interval_seconds, report_cleanup
        ),
        Periodic("import-apply", settings.import_worker_interval_seconds, import_tick),
        Periodic(
            "import-cleanup", settings.import_cleanup_interval_seconds, import_cleanup
        ),
    ]
