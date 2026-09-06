"""«Жду продолжения»: какие части франшизы считать продолжением ждавшейся страницы."""
from app.models import Page
from app.service import continuation_parts


def _p(i, year=None, finished=False, ctype="series", refreshed=True):
    return Page(id=i, hdrezka_id=1000 + i, title=f"t{i}", url="u", year=year, is_finished=finished,
                content_type=ctype)


def test_new_season_yes_old_parts_no():
    waited = _p(1, "2024", finished=True)
    tv2 = _p(2, "2026")                       # новый сезон, 1-я серия — не завершён
    tv0 = _p(3, "2019", finished=True)        # старый сезон
    old_film = _p(4, "2015", ctype="film")    # старый фильм: фильмы «завершёнными» не помечаются — отсекает год
    assert [p.id for p in continuation_parts([waited, tv2, tv0, old_film], waited)] == [2]


def test_unread_and_unknown_year_pass_through():
    waited = _p(1, "2024", finished=True)
    unread = _p(2, None, ctype=None)          # ещё не читали: вызывающий дочитает и отфильтрует повторно
    same_year_film = _p(3, "2024", ctype="film")
    assert [p.id for p in continuation_parts([waited, unread, same_year_film], waited)] == [2, 3]


def test_waited_without_year_keeps_everything_unfinished():
    waited = _p(1, None, finished=True)
    assert [p.id for p in continuation_parts([waited, _p(2, "2010"), _p(3, "2011", finished=True)], waited)] == [2]
