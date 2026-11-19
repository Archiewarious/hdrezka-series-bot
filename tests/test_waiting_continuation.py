"""«Жду продолжения»: дойдёт ли уведомление до подписчика завершённой страницы — на настоящем Postgres.

Живой случай 14–15.11.2026: двое подписались из поиска на завершённые сериалы, которых не было в каталоге бота
(«Месть Анны / Месть Аналии / Убийца ее матери» 1×56, «Биография злодея» 1×10). Карточка поиска завершённого сериала —
«Завершен (все серии)», без номера серии: страница заводится без last_season, затравка (seed_known_episodes) при
подписке не пишется. У обеих страниц в проде не было ни одной записи в episodes — единственные такие среди подписок на
страницы. С 19.11.2026 затравку пишет первое чтение подписанной страницы без записей (service.seed_from_read): чтение
ботом после подписки или поллером; чтения ради события блока её не пишут — иначе соседняя серия того же прохода стала
бы «озвучкой».

Сценарии продолжения:
  а) новый сезон на той же странице — события блока обновлений и подстраховка _catch_missed;
  б) продолжение отдельной страницей, связанной блоком частей;
  в) отдельная страница с другим основным названием (BASE_TITLE_SQL) или вовсе без блока частей.

Тесты с xfail(strict=True) показывают дыры: когда дыру закроют, тест «неожиданно пройдёт» и упадёт — пометку снять.
"""
from datetime import timedelta

import pytest
from sqlalchemy import text

import app.bot.main as main
from app import poller as poller_mod
from app import service as svc
from app.bot import guard
from app.db import session
from app.models import User
from app.poller import Poller
from app.rezka.parser import FeedItem
from test_bot_handlers import FakeCallback, site  # noqa: F401 — site: фикстура подменного сайта бота
from test_integration import (TODAY, FakeSite, _known_franchise, _new_parts_to, _notifications, _page, _parts_html,
                              _run, block)

ANNA = 69964
ANNA_TITLE = "Месть Анны / Месть Аналии / Убийца ее матери"
ANNA_URL = "https://rezka.test/series/drama/69964-mest-anny-2020.html"
VOICE = (59, "Многоголосый закадровый")
USER = 4242

VIA_SEARCH = [pytest.param(False, id="page_known"), pytest.param(True, id="via_search")]


def show_page(hid: int, title: str, seasons: dict[int, int], franchise: list = ()) -> str:
    """Страница сериала, все сезоны на ней: seasons — {сезон: серий}; franchise — [(hdrezka_id, название, год)]."""
    tr = f'<li class="b-translator__item" data-translator_id="{VOICE[0]}">{VOICE[1]}</li>'
    eps = "".join(f'<li class="b-simple_episode__item" data-season_id="{se}" data-episode_id="{e}"></li>'
                  for se, n in seasons.items() for e in range(1, n + 1))
    last = max(seasons)
    return (f'<div class="b-post__title"><h1>{title}</h1></div>'
            f"<script>initCDNSeriesEvents({hid}, {VOICE[0]}, {last}, {seasons[last]}, false)</script>"
            f"<ul>{tr}</ul><ul>{eps}</ul>{_parts_html(title, franchise)}")


CARD = FeedItem(ANNA, ANNA_TITLE, ANNA_URL, "series", "Завершен (все серии)", None, None, True,
                meta_line="2020, Колумбия, Драмы")


async def _follow_waiting(via_search: bool, bot_read: bool = True) -> None:
    """Подписка «жду продолжения» на «Месть Анны» 1×56. via_search — как в проде: страница пришла карточкой поиска,
    нажатие подписало (cb_go → subscribe_page), бот дочитал страницу после подписки (bot_read; False — лимит бота
    исчерпан, страницу прочитает поллер). Иначе страница уже была в каталоге и прочитана до подписки."""
    site = FakeSite("", {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56})})
    async with session() as s:
        s.add(User(id=USER))
        page = await svc.upsert_page_from_feed(s, CARD)
        if not via_search:
            await svc.sync_page(s, site, ANNA, ANNA_URL)
        await svc.subscribe_page(s, USER, page.id)
        await s.commit()
    if via_search and bot_read:
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


# ----------------------------------------------------------------------------- затравка по чтению страницы (19.11.2026)

async def _seeds(hid: int) -> list[tuple[int, int, int]]:
    """Записи страницы: (сезон, серия, год first_seen_at) — у затравки 1970."""
    async with session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT e.season, e.episode, CAST(extract(year FROM e.first_seen_at) AS int) FROM episodes e "
            "JOIN pages p ON p.id = e.page_id WHERE p.hdrezka_id = :h ORDER BY 1, 2"), {"h": hid})).all()]


async def _notes_count() -> int:
    async with session() as s:
        return await s.scalar(text("SELECT count(*) FROM notifications"))


async def _tap_in_search() -> None:
    """Как в проде: карточка поиска завела страницу, нажатие в выдаче — настоящий cb_go."""
    async with session() as s:
        page = await svc.upsert_page_from_feed(s, CARD)
        await s.commit()
    await main.cb_go(FakeCallback(USER, f"go:p:{page.id}"))


def test_tap_in_search_seeds_the_page_read_after_subscription(db, site):
    """cb_go → подписка → бот дочитывает страницу (_refresh_after_follow) — это чтение и пишет затравку 1×56."""
    site._pages[ANNA] = show_page(ANNA, ANNA_TITLE, {1: 56})

    async def scenario():
        await _tap_in_search()
        return await _seeds(ANNA), await _notes_count(), site.reads

    assert db(scenario) == ([(1, 56, 1970)], 0, [ANNA])


def test_poller_seeds_the_page_the_bot_did_not_read(db, site, monkeypatch):
    """Лимит бота исчерпан — страницу после подписки читает поллер (обновление подписанных): затравка пишется там,
    молча. Опоздавшее событие о новом сезоне после этого — новая серия."""
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=0, per_hour=0))

    async def scenario():
        await _tap_in_search()
        bot_reads = list(site.reads)
        await Poller(FakeSite(block({}), {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56})})).cycle()
        seeded = await _seeds(ANNA), await _notes_count()
        late = FakeSite(block({TODAY - timedelta(days=2): [(ANNA, "Месть Анны", "series", 2, 1, VOICE[1])]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 1})})
        await _run(late)
        return bot_reads, seeded, await _notifications(USER)

    bot_reads, seeded, notes = db(scenario)
    assert bot_reads == [], "бот страницу не читал"
    assert seeded == ([(1, 56, 1970)], 0), "затравка — молча"
    assert notes == [("episode", 2, 1)]


def test_event_read_does_not_seed_a_neighbour_of_the_same_pass(db):
    """Подписка без затравки, в блоке свежие 2×1 и 2×2. Чтение ради 2×1 засеяло бы 2×2 (последняя на странице) — и 2×2
    стала бы «озвучкой»: подписчикам «любой озвучки» — ничего. Чтение ради события затравку не пишет."""
    async def scenario():
        await _follow_waiting(True, bot_read=False)
        site = FakeSite(block({TODAY: [(ANNA, "Месть Анны", "series", 2, 2, VOICE[1]),        # разбирается второй
                                       (ANNA, "Месть Анны", "series", 2, 1, VOICE[1])]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 2})})
        await Poller(site).cycle()
        return site.reads, await _notifications(USER)

    reads, notes = db(scenario)
    assert reads == [ANNA], "непрочитанная страница прочитана ради события"
    assert notes == [("episode", 2, 1), ("episode", 2, 2)]


def test_deferred_event_page_is_not_seeded_by_the_same_cycle(db, monkeypatch):
    """Событие 2×1 отложено (лимит чтений цикла), а страницу в том же цикле прочитало обновление подписанных: затравка
    по ней была бы 2×1 — и в следующем цикле событие стало бы «озвучкой». Страница отложенного события в этом цикле
    не сеется; в следующем событие — новая серия."""
    async def scenario():
        await _follow_waiting(True, bot_read=False)
        site = FakeSite(block({TODAY: [(ANNA, "Месть Анны", "series", 2, 1, VOICE[1])]}),
                        {ANNA: show_page(ANNA, ANNA_TITLE, {1: 56, 2: 1})})
        poller, budget = Poller(site), poller_mod.EVENT_READS
        monkeypatch.setattr(poller_mod, "EVENT_READS", 0)
        await poller.cycle()
        first = list(site.reads), await _seeds(ANNA), await _notifications(USER)
        monkeypatch.setattr(poller_mod, "EVENT_READS", budget)
        await poller.cycle()
        return first, await _notifications(USER)

    (reads, seeds, notes), second = db(scenario)
    assert reads == [ANNA] and seeds == [] and notes == [], "страницу прочитали, но не засеяли"
    assert second == [("episode", 2, 1)]


def test_seed_only_for_a_page_subscription_without_records(db):
    """Затравка — только у страницы с подпиской на неё саму и без записей. Страница без подписок и часть, за которой
    следят только через франшизу, — без затравки. Сама затравка уведомлений не ставит."""
    async def scenario():
        alone, part, mine = 99101, 99102, 99103
        pages = {h: show_page(h, f"Сериал {h}", {1: 10}) for h in (alone, part, mine)}
        site = FakeSite("", pages)
        async with session() as s:
            s.add(User(id=USER))
            fr = await _known_franchise(s, part)
            await _page(s, part, f"Сериал {part}", franchise_id=fr.id)
            await svc.subscribe_franchise(s, USER, fr.id)
            card = FeedItem(mine, f"Сериал {mine}", f"https://rezka.test/series/x/{mine}-a.html", "series",
                            "Завершен (все серии)", None, None, True)
            own = await svc.upsert_page_from_feed(s, card)
            await svc.subscribe_page(s, USER, own.id)
            await s.commit()
        for h in (alone, part, mine):
            async with session() as s:
                await svc.sync_page(s, site, h, f"https://rezka.test/series/x/{h}-a.html")
                await s.commit()
        return await _seeds(alone), await _seeds(part), await _seeds(mine), await _notes_count()

    assert db(scenario) == ([], [], [(1, 10, 1970)], 0)


def test_new_part_with_only_a_seed_reaches_franchise_subscribers(db):
    """Новая часть франшизы, у которой есть только затравка (на неё подписались, а событий о её сериях бот не записывал):
    затравка — не серия, которую часть уже показала, — «новая часть» и подписчикам франшизы, а не только подписанным
    на другие части."""
    async def scenario():
        async with session() as s:
            s.add_all([User(id=USER), User(id=USER + 1)])
            fr = await _known_franchise(s, 99200)
            await _page(s, 99200, "Сага", last=(1, 12), rows=[(1, 12)], franchise_id=fr.id, finished=True)
            part = await _page(s, 99201, "Сага: Возмездие", last=(1, 1), franchise_id=fr.id)
            await s.execute(text("INSERT INTO franchise_members (franchise_id, hdrezka_id, announce) "
                                 "VALUES (:f, 99201, 'pending')"), {"f": fr.id})
            await svc.subscribe_page(s, USER + 1, part.id)              # затравка 1×1
            await svc.subscribe_franchise(s, USER, fr.id)
            await s.commit()
        seeds = await _seeds(99201)
        await Poller(FakeSite("", {})).announce_new_parts()
        return seeds, await _new_parts_to(USER), await _new_parts_to(USER + 1)

    seeds, franchise_sub, part_sub = db(scenario)
    assert seeds == [(1, 1, 1970)]
    assert franchise_sub == [99201], "подписчик франшизы узнал о новой части"
    assert part_sub == [], "о своей же странице не пишем"


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
