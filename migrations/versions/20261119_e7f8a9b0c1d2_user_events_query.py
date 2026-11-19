"""Что человек ввёл в поиск или какую ссылку прислал — у итога поиска и ссылки в user_events (query).

19.11.2026: новичок четыре раза подряд получил «поиск: ничего (сайт)», а что он вводил — неизвестно: баг ли это
поиска или такого на сайте нет. Тексты писем автору по-прежнему не храним. Живёт столько же — 30 дней.

Только схема: nullable-колонка без умолчания — в Postgres без перезаписи таблицы, старые строки остаются без запроса.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-11-19 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_events", sa.Column("query", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("user_events", "query")
