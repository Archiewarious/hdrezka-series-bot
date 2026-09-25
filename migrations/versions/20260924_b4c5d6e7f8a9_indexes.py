"""Индексы: подписки по человеку, «отправляется» и очистка очереди, связи обратной связи.

24.09.2026, аудит: «Мои подписки», календарь и «Новое» читали subscriptions целиком — были только частичные
уникальные индексы (user_id, page_id) и (user_id, franchise_id); возврат зависших «отправляется» и очистка
отправленного читали всю notifications; удаление обращения — всю feedback_links.

CREATE INDEX CONCURRENTLY вне транзакции (autocommit_block): таблицы не блокируются на запись, бот и поллер
работают во время миграции. IF NOT EXISTS — повторный запуск после сбоя не падает. Данные не меняются.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-24 16:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = [
    ("subs_by_user", "subscriptions", ["user_id"], None),
    ("notifications_sending", "notifications", ["next_attempt_at"], "status = 'sending'"),
    ("notifications_done", "notifications", [sa.text("coalesce(sent_at, created_at)")], "status IN ('sent', 'failed')"),
    ("feedback_links_by_feedback", "feedback_links", ["feedback_id"], None),
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, cols, where in INDEXES:
            op.create_index(name, table, cols, postgresql_concurrently=True, if_not_exists=True,
                            postgresql_where=sa.text(where) if where else None)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _, _ in reversed(INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
