"""Фикстуры — живые страницы сайта (см. tests/fixtures/README.md) и база для интеграционных тестов.

Интеграционные тесты идут только на отдельной базе, имя которой кончается на `_test`:

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/rezka_test python -m pytest -q

Без переменной они пропускаются. Локально в контейнере: см. README, раздел «Тесты».
"""
import asyncio
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parents[1]
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TEST_DB = os.getenv("TEST_DATABASE_URL")
if TEST_DB:
    if not TEST_DB.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL должен указывать на базу с именем *_test — прод не трогаем")
    os.environ["DATABASE_URL"] = TEST_DB     # до импорта app: движок создаётся из этой переменной


@pytest.fixture(scope="session")
def html():
    def load(name: str) -> str:
        return (FIXTURES / f"{name}.html").read_text()
    return load


@pytest.fixture(scope="session")
def migrated():
    if not TEST_DB:
        pytest.skip("интеграционные тесты: нужна TEST_DATABASE_URL на базу *_test")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, cwd=ROOT)
    return TEST_DB


@pytest.fixture
def db(migrated):
    """Пустая база на тест. Возвращает run(coro_fn): корутина и закрытие пула в одном цикле событий —
    движок модульный, а соединения нельзя переносить между циклами asyncio.run."""
    from sqlalchemy import text
    from app.db import engine

    def run(coro_fn):
        async def wrapped():
            try:
                return await coro_fn()
            finally:
                await engine.dispose()
        return asyncio.run(wrapped())

    async def truncate():
        async with engine.begin() as conn:
            tables = (await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))).scalars().all()
            await conn.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))

    run(truncate)
    return run
