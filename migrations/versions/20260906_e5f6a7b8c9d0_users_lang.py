"""users.lang — язык интерфейса (ru / uk / en): новым — из Telegram language_code, дальше — настройка в ⚙️

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-06 02:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('lang', sa.String(2), server_default=sa.text("'ru'"), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'lang')
