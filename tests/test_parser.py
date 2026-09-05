from datetime import date

from app.rezka.parser import (
    franchise_name, parse_episodes_html, parse_feed, parse_title_page,
)


def test_feed_cards(html):
    items = parse_feed(html("feed_animation"))
    assert len(items) == 36
    assert all(i.hdrezka_id and i.url.startswith("https://") and i.title for i in items)
    assert all(i.section == "animation" for i in items)
    with_ep = [i for i in items if i.has_episode]
    assert with_ep, "в ленте должны быть карточки с «N сезон, M серия»"
    assert all(i.season >= 1 and i.episode >= 1 for i in with_ep)
    assert any(i.meta_line and i.meta_line[:4].isdigit() for i in items), "meta_line начинается с года"
    # у карточек без подписи (фильмы) нет ни серии, ни флага «завершён»
    assert all(not i.is_finished and not i.has_episode for i in items if i.looks_like_film)


def test_title_page_ongoing(html):
    tp = parse_title_page(html("title_slime_tv4"))
    assert tp.hdrezka_id == 88337 and tp.content_type == "series"
    assert tp.title.startswith("О моём перерождении в слизь")
    assert tp.orig_title
    assert tp.default_translator == 509
    assert [t.id for t in tp.translators] == [509, 19, 224, 444, 238]
    assert tp.translators[0].name == "DEEP"
    assert tp.episodes and max(tp.episodes) == 4
    assert len(tp.franchise) >= 8 and sum(p.is_current for p in tp.franchise) == 1
    assert 88337 in tp.franchise_ids and len(tp.franchise_ids) == len(tp.franchise)
    assert tp.schedule and any(not r.aired for r in tp.schedule), "у онгоинга есть будущие серии"
    assert all(r.season == 4 for r in tp.schedule)
    # постер — полноразмерный, не og:image
    assert tp.poster_url == "https://static.hdrezka.ac/i/2026/6/19/nf93520fd29b7kd14b79i.jpg"


def test_title_page_poster_fallback_to_og(html):
    page = html("title_slime_tv4").replace('class="b-sidecover"', 'class="x-removed"')
    tp = parse_title_page(page)
    assert tp.poster_url == "https://static.hdrezka.ac/i/2026/6/19/v4b802bb44341yz87j71j.jpg"


def test_title_page_finished_by_schedule(html):
    tp = parse_title_page(html("title_solo_leveling_2"))
    assert tp.hdrezka_id == 76597 and tp.content_type == "series"
    assert (tp.current_season, tp.current_episode) == (2, 13)
    assert len(tp.schedule) == 13 and all(r.aired for r in tp.schedule)
    assert max(r.air_date for r in tp.schedule) == date(2025, 3, 30)
    assert len(tp.franchise) == 2


def test_title_page_film(html):
    tp = parse_title_page(html("title_slime_film"))
    assert tp.hdrezka_id == 56337 and tp.content_type == "film"
    assert tp.current_season is None and not tp.episodes
    assert tp.poster_url and tp.poster_url.startswith("https://static.hdrezka.ac/")


def test_ajax_episodes(html):
    eps = parse_episodes_html(html("ajax_episodes_88337_509"))
    assert eps and all(s == 4 for s, _ in eps)
    assert (4, 1) in eps


def test_franchise_name():
    titles = [("О моём перерождении в слизь [ТВ-4]", "2026"), ("О моём перерождении в слизь: Алые узы", "2022"),
              ("О моём перерождении в слизь [ТВ-1]", "2018")]
    assert franchise_name(titles) == "О моём перерождении в слизь"
    assert franchise_name([("Поднятие уровня в одиночку: Восстаньте из тени", "2025"),
                           ("Поднятие уровня в одиночку [ТВ-1]", "2024")]) == "Поднятие уровня в одиночку"
