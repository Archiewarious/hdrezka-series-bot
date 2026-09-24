"""notify_at() — когда отправлять уведомление: тихие часы, пояс, дайджест. Считается на любом моменте суток
(параметр at, миграция c5d6e7f8a9b0), а не на часах сервера в момент теста."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db import session

UTC = timezone.utc
DAY = datetime(2026, 9, 24, tzinfo=UTC)


def _utc(local_hour: float, tz: int, day: datetime = DAY) -> datetime:
    """Момент UTC, когда у человека с поясом tz на часах local_hour (дробь — минуты)."""
    return day + timedelta(hours=local_hour - tz)


def notify_at(db, tz, qf, qt, dh, at):
    async def q():
        async with session() as s:
            return await s.scalar(text(
                "SELECT notify_at(CAST(:tz AS smallint), CAST(:qf AS smallint), CAST(:qt AS smallint), "
                "CAST(:dh AS smallint), CAST(:at AS timestamptz))"), {"tz": tz, "qf": qf, "qt": qt, "dh": dh, "at": at})
    return db(q)


@pytest.mark.parametrize("local,expected_local", [
    (23.5, 24 + 8),      # 23:30 — тихо до 08:00 следующего дня
    (2.0, 8),            # 02:00 — до 08:00 этого же утра
    (8.0, None),         # 08:00 — тихие часы кончились: сразу
    (12.0, None),        # полдень — сразу
])
def test_quiet_hours_across_midnight(db, local, expected_local):
    at = _utc(local, 3)
    got = notify_at(db, 3, 23, 8, None, at)
    assert got == (at if expected_local is None else _utc(expected_local, 3))


@pytest.mark.parametrize("local,expected_local", [(12.99, None), (13.0, 15), (14.5, 15), (15.0, None), (16.0, None)])
def test_quiet_hours_within_a_day(db, local, expected_local):
    at = _utc(local, 3)
    assert notify_at(db, 3, 13, 15, None, at) == (at if expected_local is None else _utc(expected_local, 3))


@pytest.mark.parametrize("tz,expected", [
    (-5, datetime(2026, 9, 24, 13, tzinfo=UTC)),   # у него 23:00 23-го — ждём 08:00 24-го по его часам
    (3, datetime(2026, 9, 24, 5, tzinfo=UTC)),     # 07:00 — до 08:00
    (14, None),                                     # 18:00 — сразу
])
def test_time_zones(db, tz, expected):
    at = datetime(2026, 9, 24, 4, tzinfo=UTC)
    assert notify_at(db, tz, 23, 8, None, at) == (expected or at)


def test_digest_before_and_after_its_hour(db):
    assert notify_at(db, 3, 23, 8, 20, _utc(19, 3)) == _utc(20, 3), "до своего часа — сегодня"
    assert notify_at(db, 3, 23, 8, 20, _utc(21, 3)) == _utc(24 + 20, 3), "после — завтра"
    assert notify_at(db, 3, 23, 8, 20, _utc(2, 3)) == _utc(20, 3), "дайджест сильнее тихих часов"


def test_settings_off_means_now(db):
    at = _utc(2, 3)
    assert notify_at(db, 3, None, None, None, at) == at, "тихие часы выключены"
    assert notify_at(db, 3, 5, 5, None, at) == at, "пустой интервал — тихих часов нет"


def test_old_four_argument_calls_still_work(db):
    async def q():
        async with session() as s:
            return await s.scalar(text("SELECT notify_at(CAST(3 AS smallint), NULL, NULL, NULL) <= now()"))
    assert db(q) is True
