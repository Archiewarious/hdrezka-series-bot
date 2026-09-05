"""pages: poster_url, poster_file_id

Revision ID: a1f3c9d2e7b4
Revises: 2d35b4cb47dc
Create Date: 2026-09-05 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1f3c9d2e7b4'
down_revision: Union[str, None] = '2d35b4cb47dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pages', sa.Column('poster_url', sa.Text(), nullable=True))
    op.add_column('pages', sa.Column('poster_file_id', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('pages', 'poster_file_id')
    op.drop_column('pages', 'poster_url')
