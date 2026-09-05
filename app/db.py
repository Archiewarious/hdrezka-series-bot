"""Подключение к БД. Схемой владеет Alembic (migrations/), не create_all."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import cfg

engine = create_async_engine(cfg.database_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
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
