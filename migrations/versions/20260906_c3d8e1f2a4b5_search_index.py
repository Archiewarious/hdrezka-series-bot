"""локальный поиск: pg_trgm + unaccent, norm_title(), GIN-индексы по title и orig_title

Revision ID: c3d8e1f2a4b5
Revises: b7e2d4a9c1f0
Create Date: 2026-09-06 00:30:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c3d8e1f2a4b5'
down_revision: Union[str, None] = 'b7e2d4a9c1f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    # unaccent() STABLE, в индексе нужна IMMUTABLE — обёртка с явным словарём (стандартный приём).
    # Всё со схемой public: CREATE INDEX выполняет функцию с search_path = pg_catalog.
    op.execute("""
        CREATE OR REPLACE FUNCTION norm_title(t text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
        $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, coalesce(t, ''))) $$
    """)
    op.execute("CREATE INDEX pages_title_trgm ON pages USING gin (norm_title(title) public.gin_trgm_ops)")
    op.execute("CREATE INDEX pages_orig_trgm ON pages USING gin (norm_title(orig_title) public.gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pages_orig_trgm")
    op.execute("DROP INDEX IF EXISTS pages_title_trgm")
    op.execute("DROP FUNCTION IF EXISTS norm_title(text)")
