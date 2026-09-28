import asyncio
from logging.config import fileConfig

from sqlalchemy import URL, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Импорт ради регистрации всех моделей в Base.metadata: без него autogenerate
# не увидит таблицы и сочтёт их удалёнными
import quoll.attachments.models
import quoll.auth.audit_models
import quoll.auth.models
import quoll.auth.pending_actions
import quoll.catalog.models
import quoll.interactions.models
import quoll.notifications.models
import quoll.reports.models
import quoll.imports.models
import quoll.workflows.models  # noqa: F401
from alembic import context
from quoll.config import settings
from quoll.db import Base

config = context.config
# URL.create сам экранирует спецсимволы в логине и пароле, а удвоение процента
# нужно потому, что set_main_option отдаёт значение в ConfigParser с интерполяцией
db_url = URL.create(
    "postgresql+asyncpg",
    username=settings.postgres_user,
    password=settings.postgres_password,
    host=settings.postgres_host,
    port=settings.postgres_port,
    database=settings.postgres_path,
).render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
