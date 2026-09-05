"""pages: orig_title, meta_line (каталог из ленты)

Revision ID: b7e2d4a9c1f0
Revises: a1f3c9d2e7b4
Create Date: 2026-09-06 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7e2d4a9c1f0'
down_revision: Union[str, None] = 'a1f3c9d2e7b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pages', sa.Column('orig_title', sa.Text(), nullable=True))
    op.add_column('pages', sa.Column('meta_line', sa.Text(), nullable=True))
    # Очередь чтения: страницы, которых ещё не читали (каталог из ленты, части франшиз).
    op.create_index('pages_unread', 'pages', ['created_at'], postgresql_where=sa.text('page_refreshed_at IS NULL'))


def downgrade() -> None:
    op.drop_index('pages_unread', table_name='pages')
    op.drop_column('pages', 'meta_line')
    op.drop_column('pages', 'orig_title')
