"""Неудачи чтения страниц и сверки франшиз — в базе, а не в памяти поллера.

24.09.2026, аудит: недоступная страница не сдвигала время следующей попытки и снова оказывалась первой
в очереди; двух таких хватало, чтобы остальные не обновлялись никогда. Теперь у страницы счётчик неудач,
время следующей попытки и отметка «пропала с сайта» (404/410), у франшизы — счётчик и время сверки.

Только схема: новые колонки с умолчаниями, данные не меняются (в Postgres 11+ без перезаписи таблицы).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-24 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pages', sa.Column('read_failures', sa.SmallInteger(), server_default=sa.text('0'), nullable=False))
    op.add_column('pages', sa.Column('next_read_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('pages', sa.Column('gone_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('franchises', sa.Column('refresh_failures', sa.SmallInteger(), server_default=sa.text('0'), nullable=False))
    op.add_column('franchises', sa.Column('next_refresh_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('franchises', 'next_refresh_at')
    op.drop_column('franchises', 'refresh_failures')
    op.drop_column('pages', 'gone_at')
    op.drop_column('pages', 'next_read_at')
    op.drop_column('pages', 'read_failures')
