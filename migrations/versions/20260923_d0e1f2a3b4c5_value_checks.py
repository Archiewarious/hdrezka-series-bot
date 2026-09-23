"""Границы значений в самой базе: часовой пояс, часы, язык, длина фильтров озвучек.

23.09.2026, аудит защиты: значения приходят из нажатий кнопок, а callback_data присылает клиент.
Бот проверяет их сам, но база — последний рубеж: даже ошибка в коде не запишет tz_offset = 900
или фильтр из тысячи озвучек.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-23 12:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECKS = [
    ("users", "ck_users_tz", "tz_offset BETWEEN -12 AND 14"),
    ("users", "ck_users_quiet", "(quiet_from IS NULL OR quiet_from BETWEEN 0 AND 23)"
                                " AND (quiet_to IS NULL OR quiet_to BETWEEN 0 AND 23)"),
    ("users", "ck_users_digest", "digest_hour IS NULL OR digest_hour BETWEEN 0 AND 23"),
    ("users", "ck_users_lang", "lang IN ('ru', 'uk', 'en')"),
    ("users", "ck_users_voices", "default_voice_filter IS NULL OR cardinality(default_voice_filter) <= 30"),
    ("subscriptions", "ck_subscription_voices", "voice_filter IS NULL OR cardinality(voice_filter) <= 30"),
]


def upgrade() -> None:
    for table, name, expr in CHECKS:
        op.create_check_constraint(name, table, expr)


def downgrade() -> None:
    for table, name, _ in reversed(CHECKS):
        op.drop_constraint(name, table, type_="check")
