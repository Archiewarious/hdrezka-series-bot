from app.bot.search import group_hits, is_ongoing
from app.models import Page


def _p(i, fid=None, finished=False, ctype="series", last_ep=5, refreshed=True):
    p = Page(id=i, hdrezka_id=1000 + i, title=f"t{i}", url="u", franchise_id=fid, is_finished=finished,
             content_type=ctype, last_season=1, last_episode=last_ep)
    return p


def test_group_collapses_franchise_and_splits_finished():
    g = group_hits([_p(1, fid=3), _p(2, fid=3, finished=True), _p(3), _p(4, finished=True), _p(5, ctype="film"), _p(6, fid=9)])
    assert g.franchise_ids == [3, 9] and g.origin == {3: 1, 9: 6}
    assert [p.id for p in g.standalone] == [3]
    assert [p.id for p in g.waiting] == [4], "завершённый без франшизы — строка «🔔 … · завершён»"
    assert g.hidden == 1 and not g.empty, "фильм скрыт; завершённая часть франшизы схлопнута в франшизу"


def test_finished_film_and_unread_are_hidden():
    g = group_hits([_p(1, finished=True, ctype="film"), _p(2, last_ep=None, ctype=None)])
    assert g.empty and g.hidden == 2 and not g.waiting


def test_empty_and_limits():
    assert group_hits([]).empty
    assert not group_hits([_p(1, finished=True)]).empty, "только «жду продолжения» — это уже выдача, не идём на сайт"
    g = group_hits([_p(i) for i in range(10)], max_standalone=5)
    assert len(g.standalone) == 5
    g = group_hits([_p(i, finished=True) for i in range(5)], max_waiting=3)
    assert len(g.waiting) == 3 and g.hidden == 2


def test_is_ongoing():
    assert is_ongoing(_p(1))
    assert not is_ongoing(_p(1, finished=True)) and not is_ongoing(_p(1, ctype="film"))
    assert not is_ongoing(_p(1, last_ep=None)), "объявленный без серий — не предлагаем"
