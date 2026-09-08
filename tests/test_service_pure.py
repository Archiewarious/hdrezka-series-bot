from datetime import date, datetime, timedelta, timezone

from app.rezka.parser import ScheduleRow, parse_title_page
from app.models import Page
from app.service import FINISHED_AFTER_DAYS, QUIET_DAYS, finished_by_silence, schedule_finished


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


NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)


def _page(year, known_days=None, event_days=None, ctype="series"):
    p = Page(hdrezka_id=1, title="t", url="u", year=year, content_type=ctype)
    if known_days is not None:
        p.created_at = NOW - timedelta(days=known_days)
    if event_days is not None:
        p.last_event_at = NOW - timedelta(days=event_days)
    return p


def test_silence_two_years_old_part_is_finished_at_once():
    assert finished_by_silence(_page("2024"), False, NOW)                     # только что узнали — всё равно
    assert finished_by_silence(_page("2018", known_days=0), False, NOW)
    assert finished_by_silence(_page("2022-2023", known_days=0), False, NOW)  # год из «2022-2023»


def test_silence_last_year_needs_quiet_period():
    assert not finished_by_silence(_page("2025"), False, NOW)                          # created_at не загружен
    assert not finished_by_silence(_page("2025", known_days=QUIET_DAYS - 1), False, NOW)
    assert finished_by_silence(_page("2025", known_days=QUIET_DAYS), False, NOW)


def test_silence_never_for_current_year_or_unknown_year():
    assert not finished_by_silence(_page("2026", known_days=400), False, NOW)
    assert not finished_by_silence(_page(None, known_days=400), False, NOW)


def test_silence_recent_feed_episode_or_schedule_wins():
    assert not finished_by_silence(_page("2020", known_days=400, event_days=QUIET_DAYS - 1), False, NOW)
    assert finished_by_silence(_page("2020", known_days=400, event_days=QUIET_DAYS), False, NOW)
    assert not finished_by_silence(_page("2020", known_days=400), True, NOW)   # расписание есть — решает оно


def test_pick_voices_keeps_only_available():
    """Озвучка по умолчанию не должна попадать в подписку, если у тайтла её нет: иначе тишина."""
    from app.service import pick_voices
    assert pick_voices([19, 56], {56, 224}) == [56]
    assert pick_voices([19], {56, 224}) is None, "совпадений нет — значит «любая», а не пустой фильтр"
    assert pick_voices(None, {56}) is None and pick_voices([], {56}) is None
    assert pick_voices([56, 19], {19, 56}) == [19, 56]
