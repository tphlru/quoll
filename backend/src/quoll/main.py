import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from quoll.admin import admin_router
from quoll.attachments import S3StorageService, attachments_router
from quoll.auth import auth_router, users_router
from quoll.auth.bootstrap import ensure_admin_account, ensure_demo_accounts
from quoll.auth.repositories import UserRepository
from quoll.catalog import catalog_router
from quoll.config import settings
from quoll.core import AppException
from quoll.core.worker import Workers
from quoll.interactions import (
    documents_router,
    interactions_router,
    requests_router,
)
from quoll.imports import imports_router
from quoll.jobs import background_jobs
from quoll.notifications import router as notifications_router
from quoll.notifications import ws_router as notifications_ws_router
from quoll.notifications.connection_storage import ConnectionStorage
from quoll.notifications.listener import NotificationListener
from quoll.org import org_router
from quoll.reports import reports_router
from quoll.reports.worker import ReportRunner
from quoll.seed.workflow import ensure_reference_workflow
from quoll.workflows import (
    change_requests_router,
    stages_router,
    transitions_router,
    workflows_router,
)

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
for logger_name in ["httpcore", "httpx"]:
    logging.getLogger(logger_name).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _seed_demo(session_maker) -> None:
    async with session_maker() as session:
        try:
            if await ensure_reference_workflow(session):
                logger.info("Reference workflow created")
            await ensure_demo_accounts(session, UserRepository(session))
            await session.commit()
        except IntegrityError:
            # другой процесс успел раньше
            await session.rollback()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.debug("Creating database engine")
    app.state.db_engine = create_async_engine(
        f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_path}",
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    logger.debug("Creating async session maker")
    app.state.db_session_maker = async_sessionmaker(
        bind=app.state.db_engine, expire_on_commit=False
    )
    logger.debug("Initializing S3 storage service")
    app.state.s3 = S3StorageService(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket_name=settings.s3_bucket_name,
        region_name=settings.s3_region_name,
    )
    async with app.state.db_session_maker() as session:
        await ensure_admin_account(session, UserRepository(session))
        try:
            await session.commit()
        except IntegrityError:
            logger.debug("Admin projection already created by another process")
            await session.rollback()
    if settings.demo_mode:
        await _seed_demo(app.state.db_session_maker)
    await app.state.s3.ensure_bucket()
    app.state.connection_storage = ConnectionStorage()
    listener = NotificationListener(
        {
            "user": settings.postgres_user,
            "password": settings.postgres_password,
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "database": settings.postgres_path,
        },
        app.state.db_session_maker,
        app.state.connection_storage,
    )
    listener.start()
    reports = ReportRunner(
        app.state.db_session_maker, app.state.s3, settings.report_render_processes
    )
    workers = Workers(
        background_jobs(app.state.db_session_maker, reports, app.state.s3)
    )
    if settings.workers_enabled:
        workers.start()
    logger.info("Application started")

    yield

    await listener.stop()
    if settings.workers_enabled:
        await workers.stop()
    await reports.close()
    logger.debug("Disposing database engine")
    await app.state.db_engine.dispose()
    logger.info("Application stopped")


app = FastAPI(
    title="Quoll API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    logger.warning(
        f"Domain exception on {request.method} {request.url.path}: {exc.message} (HTTP {exc.status_code})"
    )
    content = {"detail": exc.message}
    if exc.code is not None:
        content["code"] = exc.code
    if exc.params is not None:
        content["params"] = exc.params
    return JSONResponse(status_code=exc.status_code, content=content)


# ограничений в схеме много
_INTEGRITY_DETAILS = {
    "23505": "Resource conflict: a record with these unique attributes already exists",
    "23503": "Resource conflict: the record is referenced by other records or references a missing one",
    "23514": "Invalid request: the record violates a domain constraint",
    "23502": "Invalid request: a required field is missing",
}


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    code = getattr(exc.orig, "sqlstate", None)
    logger.warning(
        f"Database integrity conflict on {request.method} {request.url.path} "
        f"[{code}]: {exc.orig}"
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": _INTEGRITY_DETAILS.get(
                code, "Resource conflict: the request violates a database constraint"
            )
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        f"Unhandled server error on {request.method} {request.url.path}: {exc}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Routers
app.include_router(auth_router, prefix="/auth")
app.include_router(users_router)
app.include_router(workflows_router)
app.include_router(stages_router)
app.include_router(transitions_router)
app.include_router(change_requests_router)
app.include_router(attachments_router)
app.include_router(interactions_router)
app.include_router(requests_router)
app.include_router(documents_router)
app.include_router(org_router)
app.include_router(catalog_router)
app.include_router(admin_router)
app.include_router(reports_router)
app.include_router(imports_router)
app.include_router(notifications_router)
app.include_router(notifications_ws_router)


@app.get("/", tags=["Health"])
async def root():
    return JSONResponse(content={"ok": True})


# демо-страница из репозитория, main.py лежит в backend/src/quoll
DEMO_PAGE = Path(__file__).resolve().parents[3] / "frontend" / "auth-demo.html"


@app.get("/front")
async def frontend():
    return FileResponse(DEMO_PAGE)
