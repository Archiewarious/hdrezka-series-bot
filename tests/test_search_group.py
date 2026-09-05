from app.bot.search import group_hits, is_ongoing
from app.models import Page


def _p(i, fid=None, finished=False, ctype="series", last_ep=5, refreshed=True):
    p = Page(id=i, hdrezka_id=1000 + i, title=f"t{i}", url="u", franchise_id=fid, is_finished=finished,
             content_type=ctype, last_season=1, last_episode=last_ep)
    return p


def test_group_collapses_franchise_and_hides_finished():
    g = group_hits([_p(1, fid=3), _p(2, fid=3, finished=True), _p(3), _p(4, finished=True), _p(5, ctype="film"), _p(6, fid=9)])
    assert g.franchise_ids == [3, 9] and g.origin == {3: 1, 9: 6}
    assert [p.id for p in g.standalone] == [3]
    assert g.hidden == 2 and not g.empty


def test_empty_and_limits():
    assert group_hits([]).empty
    assert group_hits([_p(1, finished=True)]).empty
    g = group_hits([_p(i) for i in range(10)], max_standalone=5)
    assert len(g.standalone) == 5


def test_is_ongoing():
    assert is_ongoing(_p(1))
    assert not is_ongoing(_p(1, finished=True)) and not is_ongoing(_p(1, ctype="film"))
    assert not is_ongoing(_p(1, last_ep=None)), "объявленный без серий — не предлагаем"
