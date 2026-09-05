from datetime import date, timedelta

from app.rezka.parser import ScheduleRow, parse_title_page
from app.service import FINISHED_AFTER_DAYS, schedule_finished


def _row(days_ago: int, aired: bool = True) -> ScheduleRow:
    return ScheduleRow(1, 1, None, date.today() - timedelta(days=days_ago), "✓" if aired else "", aired)


def test_finished_when_all_aired_long_ago():
    assert schedule_finished([_row(400), _row(393)])


def test_not_finished_recently_or_with_future():
    assert not schedule_finished([_row(FINISHED_AFTER_DAYS - 1)])
    assert not schedule_finished([_row(400), _row(-7, aired=False)])
    assert not schedule_finished([])


def test_unknown_dates_are_not_finished():
    assert not schedule_finished([ScheduleRow(1, 1, None, None, "✓", True)])


def test_live_pages(html):
    assert schedule_finished(parse_title_page(html("title_solo_leveling_2")).schedule)
    assert not schedule_finished(parse_title_page(html("title_slime_tv4")).schedule)


def test_year_from_meta():
    from app.service import year_from_meta
    assert year_from_meta("2026, Япония, Фэнтези") == "2026"
    assert year_from_meta("2024 - 2025, США, Драмы") == "2024"
    assert year_from_meta(None) is None and year_from_meta("Япония") is None
