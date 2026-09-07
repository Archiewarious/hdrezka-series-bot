"""Что показывает карточка сериала: одна кнопка «Следить», состояние подписки видно всегда."""
from app.models import Page
from app.service import card_state


def _p(finished=False, ctype="series"):
    return Page(id=1, hdrezka_id=1, title="t", url="u", is_finished=finished, content_type=ctype)


def test_follow_is_default_for_airing():
    assert card_state(_p(), page_sub=False, franchise_sub=False, franchise=False) == "follow"
    assert card_state(_p(), page_sub=False, franchise_sub=False, franchise=True) == "follow"


def test_own_subscription_wins_over_franchise():
    assert card_state(_p(), page_sub=True, franchise_sub=True, franchise=True) == "subscribed"
    assert card_state(_p(finished=True), page_sub=True, franchise_sub=True, franchise=True) == "waiting"


def test_franchise_subscription_is_visible():
    """Раньше здесь была «Найдено:» без кнопок — человек не понимал, подписан он или нет."""
    assert card_state(_p(), page_sub=False, franchise_sub=True, franchise=True) == "franchise_sub"
    assert card_state(_p(finished=True), page_sub=False, franchise_sub=True, franchise=True) == "franchise_sub"


def test_finished_and_films():
    assert card_state(_p(finished=True), False, False, franchise=False) == "finished_alone"
    assert card_state(_p(finished=True), False, False, franchise=True) == "finished_franchise"
    assert card_state(_p(ctype="film"), False, False, franchise=True) == "film"
    assert card_state(_p(finished=True, ctype="film"), page_sub=True, franchise_sub=False,
                      franchise=False) == "subscribed", "фильм не бывает «жду продолжения»"
