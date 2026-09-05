"""Фикстуры — живые страницы сайта, сохранённые 05.09.2026 (см. tests/fixtures/README.md)."""
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def html():
    def load(name: str) -> str:
        return (FIXTURES / f"{name}.html").read_text()
    return load
