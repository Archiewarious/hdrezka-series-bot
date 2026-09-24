"""Alembic: схема живёт в app.models, URL — в переменной окружения DATABASE_URL."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import cfg
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", cfg.database_url)
target_metadata = Base.metadata

# Индексы поиска созданы сырым SQL (миграция c3d8e1f2a4b5): выражение norm_title() с gin_trgm_ops в модели не
# описать, и автогенерация считала бы их лишними и удаляла (24.09.2026).
SQL_ONLY_INDEXES = {"pages_title_trgm", "pages_orig_trgm"}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "index" and name in SQL_ONLY_INDEXES)


def run_migrations_offline() -> None:
    context.configure(url=cfg.database_url, target_metadata=target_metadata, include_object=include_object,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(config.get_section(config.config_ini_section, {}),
                                      prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
