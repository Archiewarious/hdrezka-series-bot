"""Состав франшиз (franchise_members) и возврат в очередь чтения страниц со стёртым типом.

24.09.2026, аудит: новой частью франшизы считалась только страница, которой не было в базе до чтения
блока частей. Бот заносит страницы раньше (поиск, ссылка), и уведомления о новом фильме не было никогда.
Теперь новое — то, чего не было в составе франшизы. Текущий состав записывается как baseline:
пропущенные раньше части задним числом не рассылаются (решение владельца 4).

Поиск по сайту стирал content_type у прочитанных фильмов (карточка без подписи) — такие страницы
дочитает очередь каталога.

Данные: заполняет franchise_members из pages; сбрасывает page_refreshed_at у страниц без типа.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-24 10:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'franchise_members',
        sa.Column('franchise_id', sa.Integer(), sa.ForeignKey('franchises.id', ondelete='CASCADE'), nullable=False),
        sa.Column('hdrezka_id', sa.Integer(), nullable=False),
        sa.Column('seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('announce', sa.String(8), server_default=sa.text("'baseline'"), nullable=False),
        sa.PrimaryKeyConstraint('franchise_id', 'hdrezka_id'),
        sa.CheckConstraint("announce IN ('baseline', 'pending', 'sent', 'skipped')", name='ck_franchise_member_announce'),
    )
    op.create_index('franchise_members_pending', 'franchise_members', ['seen_at'],
                    postgresql_where=sa.text("announce = 'pending'"))
    op.execute("""
        INSERT INTO franchise_members (franchise_id, hdrezka_id, announce)
        SELECT franchise_id, hdrezka_id, 'baseline' FROM pages WHERE franchise_id IS NOT NULL
        ON CONFLICT DO NOTHING""")
    op.execute("UPDATE pages SET page_refreshed_at = NULL WHERE content_type IS NULL AND page_refreshed_at IS NOT NULL")


def downgrade() -> None:
    op.drop_index('franchise_members_pending', table_name='franchise_members')
    op.drop_table('franchise_members')
