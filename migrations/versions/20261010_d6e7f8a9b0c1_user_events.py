"""Действия людей в боте (user_events): тип и короткая метка, без текстов, 30 дней.

10.10.2026: пришёл новый человек, ничего не подписал — и понять, что он делал и где потерялся, было не по чему.
Таблица новая и пустая: индексы создаются сразу, без CONCURRENTLY.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-10 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("user_events_by_user", "user_events", ["user_id", "at"])
    op.create_index("user_events_by_time", "user_events", ["at"])


def downgrade() -> None:
    op.drop_table("user_events")
