"""notifications.tg_message_id — номер сообщения Telegram у отправленного уведомления.

11.09.2026 пользователь не получил двух уведомлений, которые база отметила отправленными; без номера сообщения
доставку было нечем ни доказать, ни опровергнуть.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-11 16:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('tg_message_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('notifications', 'tg_message_id')
