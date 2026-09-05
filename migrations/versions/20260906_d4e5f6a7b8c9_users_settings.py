"""users: настройки (картинки, часовой пояс, тихие часы, дайджест, озвучка по умолчанию) + notify_at()

Revision ID: d4e5f6a7b8c9
Revises: c3d8e1f2a4b5
Create Date: 2026-09-06 01:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d8e1f2a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Когда отправлять уведомление этому человеку: дайджест — в его час; тихие часы — утром;
# иначе сразу. Считается один раз при постановке в очередь (next_attempt_at), sender ничего не знает.
NOTIFY_AT = """
CREATE OR REPLACE FUNCTION notify_at(tz smallint, qf smallint, qt smallint, dh smallint)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  loc timestamp := timezone('UTC', now()) + make_interval(hours => tz);   -- местное время, без зоны
  h   int := extract(hour from loc);
  due timestamp;
BEGIN
  IF dh IS NOT NULL THEN
    due := date_trunc('day', loc) + make_interval(hours => dh);
  ELSIF qf IS NOT NULL AND qt IS NOT NULL AND qf <> qt
        AND ((qf > qt AND (h >= qf OR h < qt)) OR (qf < qt AND h >= qf AND h < qt)) THEN
    due := date_trunc('day', loc) + make_interval(hours => qt);
  ELSE
    RETURN now();
  END IF;
  IF due <= loc THEN due := due + interval '1 day'; END IF;
  RETURN timezone('UTC', due - make_interval(hours => tz));
END $$
"""


def upgrade() -> None:
    op.add_column('users', sa.Column('photos', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('users', sa.Column('tz_offset', sa.SmallInteger(), server_default=sa.text('3'), nullable=False))
    op.add_column('users', sa.Column('quiet_from', sa.SmallInteger(), server_default=sa.text('23'), nullable=True))
    op.add_column('users', sa.Column('quiet_to', sa.SmallInteger(), server_default=sa.text('8'), nullable=True))
    op.add_column('users', sa.Column('digest_hour', sa.SmallInteger(), nullable=True))
    op.add_column('users', sa.Column('default_voice_filter', postgresql.ARRAY(sa.Integer()), nullable=True))
    op.execute(NOTIFY_AT)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS notify_at(smallint, smallint, smallint, smallint)")
    for col in ('default_voice_filter', 'digest_hour', 'quiet_to', 'quiet_from', 'tz_offset', 'photos'):
        op.drop_column('users', col)
