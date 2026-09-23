"""Подключение к БД. Схемой владеет Alembic (migrations/), не create_all."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import cfg

# Зависший запрос или брошенная транзакция не должны держать соединение и блокировки вечно: пул на процесс —
# 30 соединений, у Postgres их 100 на всех. Поллер между запросами к базе ждёт сайт (до 25 с на запрос,
# с повторами и сменой зеркала дольше), поэтому простой в транзакции — с большим запасом.
DB_TIMEOUTS = {"statement_timeout": "60000", "idle_in_transaction_session_timeout": "600000"}   # мс
engine = create_async_engine(cfg.database_url, pool_size=10, max_overflow=20, pool_pre_ping=True,
                             connect_args={"server_settings": DB_TIMEOUTS, "command_timeout": 90})
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Проверка соединения при старте процесса. Таблицы создаёт `alembic upgrade head`
    (сервис migrate в docker-compose), поэтому здесь ничего не создаём."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s
