"""«Жду продолжения»: дойдёт ли уведомление до подписчика завершённой страницы — на настоящем Postgres.

Живой случай 14–15.11.2026: двое подписались из поиска на завершённые сериалы, которых не было в каталоге бота
(«Месть Анны / Месть Аналии / Убийца ее матери» 1×56, «Биография злодея» 1×10). Карточка поиска завершённого сериала —
«Завершен (все серии)», без номера серии: страница заводится без last_season, затравка (seed_known_episodes) при
подписке не пишется, а чтение страницы после подписки (_refresh_after_follow) её уже не пишет. У обеих страниц в проде
нет ни одной записи в episodes — единственные такие среди подписок на страницы.

Сценарии продолжения:
  а) новый сезон на той же странице — события блока обновлений и подстраховка _catch_missed;
  б) продолжение отдельной страницей, связанной блоком частей;
  в) отдельная страница с другим основным названием (BASE_TITLE_SQL) или вовсе без блока частей.

Тесты с xfail(strict=True) показывают дыры: когда дыру закроют, тест «неожиданно пройдёт» и упадёт — пометку снять.
"""
from datetime import timedelta

import pytest
from sqlalchemy import text

from app import service as svc
from app.db import session
from app.models import User
from app.poller import Poller
from app.rezka.parser import FeedItem
from test_integration import TODAY, FakeSite, _new_parts_to, _notifications, _parts_html, _run, block

ANNA = 69964
ANNA_TITLE = "Месть Анны / Месть Аналии / Убийца ее матери"
ANNA_URL = "https://rezka.test/series/drama/69964-mest-anny-2020.html"
VOICE = (59, "Многоголосый закадровый")
USER = 4242

NO_SEED = ("подписка из поиска на завершённую страницу без затравки: карточка «Завершен (все серии)» без номера серии, "
           "seed_known_episodes ничего не пишет, чтение после подписки — тоже")
VIA_SEARCH = [pytest.param(False, id="page_known"),
              pytest.param(True, id="via_search", marks=pytest.mark.xfail(strict=True, reason=NO_SEED))]


def show_page(hid: int, title: str, seasons: dict[int, int], franchise: list = ()) -> str:
    """Страница сериала, все сезоны на ней: seasons — {сезон: серий}; franchise — [(hdrezka_id, название, год)]."""
    tr = f'<li class="b-translator__item" data-translator_id="{VOICE[0]}">{VOICE[1]}</li>'
    eps = "".join(f'<li class="b-simple_episode__item" data-season_id="{se}" data-episode_id="{e}"></li>'
                  for se, n in seasons.items() for e in range(1, n + 1))
    last = max(seasons)
    return (f'<div class="b-post__title"><h1>{title}</h1></div>'
            f"<script>initCDNSeriesEvents({hid}, {VOICE[0]}, {last}, {seasons[last]}, false)</script>"
            f"<ul>{tr}</ul><ul>{eps}</ul>{_parts_html(title, franchise)}")


async def _follow_waiting(via_search: bool) -> None:
    """Подписка «жду продолжения» на «Месть Анны» 1×56. via_search — как в проде: страница пришла карточкой поиска,
    нажатие подписало (cb_go → subscribe_page), бот дочитал страницу после подписки. Иначе страница уже была в каталоге
    и прочитана до подписки."""
    site = FakeSite("", {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56})})
    card = FeedItem(ANNA, ANNA_TITLE, ANNA_URL, "series", "Завершен (все серии)", None, None, True,
                    meta_line="2020, Колумбия, Драмы")
    async with session() as s:
        s.add(User(id=USER))
        page = await svc.upsert_page_from_feed(s, card)
        if not via_search:
            await svc.sync_page(s, site, ANNA, ANNA_URL)
        await svc.subscribe_page(s, USER, page.id)
        await s.commit()
    if via_search:
        async with session() as s:                          # _refresh_after_follow
            await svc.sync_page(s, site, ANNA, ANNA_URL)
            await s.commit()


async def _age_page(hid: int, days: int) -> None:
    """Страница прочитана давно — завершённую с подпиской перечитывают раз в неделю (refresh_pages)."""
    async with session() as s:
        await s.execute(text("UPDATE pages SET page_refreshed_at = now() - make_interval(days => :d) WHERE hdrezka_id = :h"),
                        {"d": days, "h": hid})
        await s.commit()


# ----------------------------------------------------------------------------- корень: нет затравки

@pytest.mark.parametrize("via_search", VIA_SEARCH)
def test_waiting_subscription_has_a_seed(db, via_search):
    """Как 14–15.11.2026 в проде: после подписки и чтения страницы у неё есть запись 1×56 «уже была»."""
    async def scenario():
        await _follow_waiting(via_search)
        async with session() as s:
            return (await s.execute(text(
                "SELECT e.season, e.episode FROM episodes e JOIN pages p ON p.id = e.page_id WHERE p.hdrezka_id = :h"),
                {"h": ANNA})).all()

    assert db(scenario) == [(1, 56)]


# ----------------------------------------------------------------------------- а) новый сезон на той же странице

@pytest.mark.parametrize("via_search", [False, True])
def test_new_season_fresh_event_reaches_waiting_subscriber(db, via_search):
    """Основной путь работает и без затравки: свежее событие 2×1 — «new» (записей нет, событие сегодняшнее)."""
    async def scenario():
        await _follow_waiting(via_search)
        site = FakeSite(block({TODAY: [(ANNA, "Месть Анны", "series", 2, 1, VOICE[1])]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 1})})
        await _run(site)
        return await _notifications(USER)

    assert db(scenario) == [("episode", 2, 1)]


@pytest.mark.parametrize("via_search", VIA_SEARCH)
def test_new_season_late_event_reaches_waiting_subscriber(db, via_search):
    """Событие 2×1 разобрано позже «вчера» (поллер лежал больше суток, бан, событие откладывалось — в том числе
    будущим подтверждающим чтением страницы). С затравкой 2×1 > 1×56 — «new»; без неё — «catchup», молча."""
    async def scenario():
        await _follow_waiting(via_search)
        site = FakeSite(block({TODAY - timedelta(days=2): [(ANNA, "Месть Анны", "series", 2, 1, VOICE[1])]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 1})})
        await _run(site)
        return await _notifications(USER)

    assert db(scenario) == [("episode", 2, 1)]


@pytest.mark.parametrize("via_search", VIA_SEARCH)
def test_new_season_missed_by_the_block_is_caught_on_weekly_reread(db, via_search):
    """Сезон появился на странице без события в блоке. Подстраховка _catch_missed при еженедельном перечитывании
    сравнивает список серий с записями — без записей (page_max None) она молчит."""
    async def scenario():
        await _follow_waiting(via_search)
        await _age_page(ANNA, 8)
        site = FakeSite("", {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 3})})
        await Poller(site).refresh_pages()
        return site.reads, await _notifications(USER)

    reads, notes = db(scenario)
    assert reads == [ANNA], "страница перечитана"
    assert notes == [("episode", 2, 3)]


@pytest.mark.parametrize("via_search", VIA_SEARCH)
def test_new_dub_of_an_old_episode_is_silent(db, via_search):
    """Обратная сторона той же дыры: новая озвучка старой серии 1×30 без затравки — «new» (записей нет, событие
    свежее), и человек получает «вышла 1×30» как новую серию. Ради этого затравка и заведена."""
    async def scenario():
        await _follow_waiting(via_search)
        site = FakeSite(block({TODAY: [(ANNA, "Месть Анны", "series", 1, 30, "Оригинал (+субтитры)")]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56})})
        await _run(site)
        return await _notifications(USER)

    assert db(scenario) == []


# ----------------------------------------------------------------------------- б, в) продолжение отдельной страницей

SEQUEL = 99001


@pytest.mark.parametrize("sequel_title", [
    pytest.param("Месть Анны: Возмездие", id="same_base"),
    pytest.param("Месть Анны 2 / Месть Аналии 2 / Убийца ее матери 2", id="title_2", marks=pytest.mark.xfail(
        strict=True, reason="BASE_TITLE_SQL: «месть анны 2» ≠ «месть анны» — так на сайте названы «Женская логика 2», "
                            "«Телохранитель 2»")),
    pytest.param("Месть Аналии 2", id="second_alias", marks=pytest.mark.xfail(
        strict=True, reason="основное название берётся только до первого «/» — другие названия страницы не сравниваются")),
])
def test_sequel_page_linked_by_franchise_reaches_waiting_subscriber(db, sequel_title):
    """Продолжение — новая страница, сайт связал её блоком частей; первая серия свежим событием. Франшизы до этого
    не было: её заводит чтение новой страницы, ждущему уходит «новая часть», если основные названия совпали."""
    async def scenario():
        await _follow_waiting(True)
        site = FakeSite(block({TODAY: [(SEQUEL, sequel_title, "series", 1, 1, VOICE[1])]}), {
            SEQUEL: show_page(SEQUEL, sequel_title, {1: 1}, [(ANNA, ANNA_TITLE, 2020)]),
            ANNA: show_page(ANNA, ANNA_TITLE, {1: 56}, [(SEQUEL, sequel_title, TODAY.year)])})
        await _run(site)
        return site.reads, await _new_parts_to(USER)

    reads, parts = db(scenario)
    assert reads == [SEQUEL], "непрочитанная страница с новой серией читается до уведомлений"
    assert parts == [SEQUEL]


@pytest.mark.xfail(strict=True, reason="бот связывает продолжение со страницей только через блок частей сайта")
def test_sequel_page_without_franchise_block_reaches_waiting_subscriber(db):
    """в) Сайт выложил продолжение отдельной страницей и не связал их: «новой части» нет — франшизы нет."""
    async def scenario():
        await _follow_waiting(True)
        title = "Месть Анны: Возмездие"
        site = FakeSite(block({TODAY: [(SEQUEL, title, "series", 1, 1, VOICE[1])]}),
                        {SEQUEL: show_page(SEQUEL, title, {1: 1})})
        await _run(site)
        return await _new_parts_to(USER)

    assert db(scenario) == [SEQUEL]


@pytest.mark.xfail(strict=True, reason="франшиза заводится тем же чтением, которое показало продолжение: «при первом "
                                       "знакомстве все части — не новость» (baseline), объявлять нечего")
def test_sequel_revealed_by_weekly_reread_without_events_reaches_waiting_subscriber(db):
    """б) Продолжение выложено целиком, в блок обновлений не попало. Бот узнаёт о нём, перечитывая ждущую страницу
    (раз в неделю): у неё впервые появился блок частей. Подписчику — ничего: оба члена новой франшизы baseline."""
    async def scenario():
        await _follow_waiting(False)
        await _age_page(ANNA, 8)
        title = "Месть Анны: Возмездие"
        site = FakeSite(block({}), {
            ANNA: show_page(ANNA, ANNA_TITLE, {1: 56}, [(SEQUEL, title, TODAY.year)]),
            SEQUEL: show_page(SEQUEL, title, {1: 60}, [(ANNA, ANNA_TITLE, 2020)])})
        poller = Poller(site)
        for _ in range(3):
            await poller.cycle()
        async with session() as s:
            statuses = sorted((await s.execute(text("SELECT announce FROM franchise_members"))).scalars())
        return statuses, await _new_parts_to(USER)

    statuses, parts = db(scenario)
    assert parts == [SEQUEL], f"состав франшизы: {statuses}"


@pytest.mark.parametrize("years_ago", [
    pytest.param(1, id="last_year"),
    pytest.param(2, id="two_years_ago", marks=pytest.mark.xfail(
        strict=True, reason="is_recent_part: год части из блока старше прошлого — «старая», хотя на сайте она появилась "
                            "только сейчас (сезон 2025 года, выложенный в 2027-м)")),
])
def test_sequel_from_an_earlier_year_uploaded_now_reaches_waiting_subscriber(db, years_ago):
    """«Месть Аналии 2» вышла в Колумбии в 2025-м. Если сайт выложит её с 2027-го и бот сначала увидит её в блоке частей
    ждущей страницы (с годом), первая серия свежим событием «новой частью» не станет."""
    async def scenario():
        await _follow_waiting(False)
        await _age_page(ANNA, 8)
        title = "Месть Анны: Возмездие"
        pages = {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56}, [(SEQUEL, title, TODAY.year - years_ago)]),
                 SEQUEL: show_page(SEQUEL, title, {1: 1}, [(ANNA, ANNA_TITLE, 2020)])}
        await Poller(FakeSite(block({}), pages)).cycle()                     # перечитали ждущую — блок частей
        await _run(FakeSite(block({TODAY: [(SEQUEL, title, "series", 1, 1, VOICE[1])]}), pages))
        return await _new_parts_to(USER)

    assert db(scenario) == [SEQUEL]
