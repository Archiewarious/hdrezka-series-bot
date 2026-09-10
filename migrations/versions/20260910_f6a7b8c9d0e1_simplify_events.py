"""Упрощение источника событий (10.09.2026): отложенные проверки озвучек через плеер больше не нужны —
блок обновлений сообщает озвучку каждого события; служебные ключи курсора и сторожа удалены; подписанным
страницам без записей о сериях записывается текущая последняя серия как уже известная (затравка, 1970).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-10 18:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('voice_checks')
    op.execute("DELETE FROM meta WHERE key LIKE 'feed_cursor:%' OR key IN ('updates_baseline', 'poller_stale')")
    op.execute("""
        INSERT INTO episodes (page_id, season, episode, first_seen_at)
        SELECT p.id, p.last_season, p.last_episode, timestamptz '1970-01-01 00:00:00+00'
          FROM pages p
         WHERE p.last_season IS NOT NULL AND p.last_episode IS NOT NULL
           AND EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.page_id = p.id OR sub.franchise_id = p.franchise_id)
           AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.page_id = p.id)
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.create_table(
        'voice_checks',
        sa.Column('episode_id', sa.Integer(), sa.ForeignKey('episodes.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('translator_id', sa.Integer(), primary_key=True),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.create_index('voice_checks_due', 'voice_checks', ['next_check_at'])
