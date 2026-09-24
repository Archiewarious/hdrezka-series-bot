"""Конфигурация (шаг 9 аудита, 24.09.2026): пароль базы в одном месте, понятные ошибки настройки."""
import pytest

from app import config


def test_database_url_is_built_from_postgres_password(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/wo:rd")
    assert config._database_url() == "postgresql+asyncpg://rezka:p%40ss%2Fwo%3Ard@postgres:5432/rezka"


def test_explicit_database_url_wins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "другой")
    assert config._database_url() == "postgresql+asyncpg://u:p@db:5432/x"


def test_bad_admin_ids_say_what_is_wrong(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "224606361, @alex")
    with pytest.raises(SystemExit) as exc:
        config._admin_ids()
    assert "ADMIN_IDS" in str(exc.value) and "@alex" in str(exc.value)
    monkeypatch.setenv("ADMIN_IDS", " 1, 2 ,")
    assert config._admin_ids() == [1, 2]
