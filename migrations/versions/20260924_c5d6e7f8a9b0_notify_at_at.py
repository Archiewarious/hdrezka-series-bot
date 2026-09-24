"""notify_at(): необязательный параметр at — момент, от которого считать (по умолчанию now()).

24.09.2026, аудит: тихие часы, пояса и дайджест проверялись только тем, что показывали часы сервера в момент
теста. С параметром at функция проверяется на любом моменте суток. Старые вызовы с четырьмя аргументами не
меняются. Старую функцию на четыре аргумента удаляем в той же транзакции: рядом с новой (пятый — с умолчанием)
вызов notify_at(a, b, c, d) стал бы неоднозначным.

Данные не меняются.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-24 18:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Когда отправлять уведомление этому человеку: дайджест — в его час; тихие часы — утром; иначе сразу.
BODY = """
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  loc timestamp := timezone('UTC', {now}) + make_interval(hours => tz);   -- местное время, без зоны
  h   int := extract(hour from loc);
  due timestamp;
BEGIN
  IF dh IS NOT NULL THEN
    due := date_trunc('day', loc) + make_interval(hours => dh);
  ELSIF qf IS NOT NULL AND qt IS NOT NULL AND qf <> qt
        AND ((qf > qt AND (h >= qf OR h < qt)) OR (qf < qt AND h >= qf AND h < qt)) THEN
    due := date_trunc('day', loc) + make_interval(hours => qt);
  ELSE
    RETURN {now};
  END IF;
  IF due <= loc THEN due := due + interval '1 day'; END IF;
  RETURN timezone('UTC', due - make_interval(hours => tz));
END $$
"""


def upgrade() -> None:
    op.execute("DROP FUNCTION notify_at(smallint, smallint, smallint, smallint)")
    op.execute("CREATE FUNCTION notify_at(tz smallint, qf smallint, qt smallint, dh smallint, "
               "at timestamptz DEFAULT now())" + BODY.format(now="at"))


def downgrade() -> None:
    op.execute("DROP FUNCTION notify_at(smallint, smallint, smallint, smallint, timestamptz)")
    op.execute("CREATE FUNCTION notify_at(tz smallint, qf smallint, qt smallint, dh smallint)"
               + BODY.format(now="now()"))
