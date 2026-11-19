"""Поллер: очередь каталога, сверка франшиз, границы транзакций цикла, паузы при потере доступа, сверка скачков
из блока обновлений со страницей. Сайт подменён (FakeSite из test_integration)."""
import asyncio
import dataclasses
import logging
from datetime import timedelta

from sqlalchemy import text

from app import lifecycle
from app import poller as poller_mod
from app import service as svc
from app.db import session
from app.models import Franchise, Subscription, User
from app.poller import Poller
from app.rezka.client import AccessBlocked
from test_integration import TODAY, FakeSite, _known_franchise, _notifications, _page, block, title_page

VOICES = [(56, "Дубляж", None)]


def test_catalog_queue_reads_subscribed_first_and_skips_not_due(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=81))
            await _page(s, 8101, "Новая из поиска", read=False)
            sub = await _page(s, 8102, "С подпиской", read=False)
            later = await _page(s, 8103, "Ждёт попытки", read=False)
            later.next_read_at = svc.now() + timedelta(hours=2)
            await s.flush()
            s.add(Subscription(user_id=81, scope="page", page_id=sub.id))
            await s.commit()
        site = FakeSite("", {h: title_page(h, str(h), 1, 1, VOICES) for h in (8101, 8102, 8103)})
        done = await Poller(site).read_pending_pages()
        return done, site.reads

    assert db(scenario) == (2, [8102, 8101]), "сначала с подписчиками; неудачная ждёт своего времени"


def test_franchises_to_check_and_their_failures(db):
    """Сверяются франшизы с подписчиками и с «ждущими продолжения»; потеря доступа отмечает франшизу."""
    async def scenario():
        async with session() as s:
            s.add_all([User(id=82), User(id=83)])
            with_sub = await _known_franchise(s, 8201)
            waiting = await _known_franchise(s, 8301)
            nobody = await _known_franchise(s, 8401)
            await _page(s, 8201, "A", last=(1, 2), franchise_id=with_sub.id)
            done = await _page(s, 8301, "B", last=(1, 12), franchise_id=waiting.id, finished=True)
            await _page(s, 8401, "C", last=(1, 2), franchise_id=nobody.id)
            await svc.subscribe_franchise(s, 82, with_sub.id)
            s.add(Subscription(user_id=83, scope="page", page_id=done.id))
            await s.commit()
        site = FakeSite("", {8201: AccessBlocked("403"), 8301: title_page(8301, "B", 1, 12, VOICES)})
        poller = Poller(site)
        try:
            await poller.refresh_franchises()
        except AccessBlocked:
            pass
        async with session() as s:
            fails = dict((await s.execute(text(
                "SELECT key_hdrezka_id, (refresh_failures, next_refresh_at > now()) FROM franchises"))).all())
        return site.reads, fails

    reads, fails = db(scenario)
    assert reads == [8201], "по порядку: первая — с подписчиком; после потери доступа часть цикла закончена"
    assert fails[8201] == (1, True) and fails[8301] == (0, None) and fails[8401] == (0, None), \
        "отмечена только та, что не сверилась; время попытки у остальных не ставилось"


def test_cycle_commits_events_even_if_the_second_part_loses_access(db):
    """Блок обновлений разобран и записан, last_poll_ok стоит; потеря доступа во второй части его не откатывает,
    а refresh_ok_at не обновляется."""
    async def scenario():
        async with session() as s:
            s.add(User(id=84))
            page = await _page(s, 8500, "Сериал", last=(1, 3), rows=[(1, 3)], voices=[(56, "Дубляж")])
            page.page_refreshed_at = svc.now() - timedelta(days=1)
            await svc.subscribe_page(s, 84, page.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(8500, "Сериал", "series", 1, 4, "Дубляж")]}), {8500: AccessBlocked("403")})
        await Poller(site).cycle()
        async with session() as s:
            eps = (await s.execute(text("SELECT count(*) FROM episodes WHERE episode = 4"))).scalar()
            notes = (await s.execute(text("SELECT count(*) FROM notifications"))).scalar()
            return eps, notes, await svc.meta_get(s, "last_poll_ok") is not None, await svc.meta_get(s, "refresh_ok_at")

    assert db(scenario) == (1, 1, True, None)


def test_run_pauses_after_lost_access_and_resets_after_success(db, monkeypatch):
    pauses, results = [], [AccessBlocked("403"), AccessBlocked("403"), None]

    async def fake_cycle(self):
        r = results.pop(0)
        if not results:
            lifecycle.request_stop()
        if r:
            raise r

    async def record(seconds, beat=None):
        pauses.append(seconds)
    monkeypatch.setattr(Poller, "cycle", fake_cycle)
    monkeypatch.setattr(lifecycle, "pause", record)

    async def scenario():
        await Poller(FakeSite("", {})).run()

    db(scenario)
    # После последнего цикла пауза тоже вызывается — настоящая при сигнале остановки возвращается сразу.
    assert pauses == [120, 180, 240, 180, 180], "пауза после потери доступа растёт, между циклами — интервал опроса"


def test_ban_is_visible_in_health_until_access_returns(db, monkeypatch):
    """03.10.2026: туннель снят, ходим напрямую, а бан IP сервера может вернуться. В Telegram о нём не пишем — значит,
    он обязан быть в проверке здоровья (/stats, healthcheck поллера), пока доступ не вернётся."""
    from app import health
    seen, results = [], [AccessBlocked("https://rezka.test/: 403", banned=True), AccessBlocked("таймаут"), None]

    async def access_problems():
        async with session() as s:
            return [p for p in await health.check(s, part="events") if "403" in p or "доступа" in p]

    async def fake_cycle(self):
        seen.append(await access_problems())
        r = results.pop(0)
        if not results:
            lifecycle.request_stop()
        if r:
            raise r

    async def no_pause(seconds, beat=None):
        pass
    monkeypatch.setattr(Poller, "cycle", fake_cycle)
    monkeypatch.setattr(lifecycle, "pause", no_pause)
    monkeypatch.setattr(health, "cfg", dataclasses.replace(health.cfg, proxy=None))

    async def scenario():
        await Poller(FakeSite("", {})).run()
        return await access_problems()

    after = db(scenario)
    assert seen[0] == []
    assert len(seen[1]) == 1 and seen[1][0].startswith("БАН: сайт отдаёт 403 уже 0 мин, выход напрямую")
    assert seen[2] == ["нет доступа к сайту уже 0 мин, выход напрямую: таймаут"], "не бан — без совета про прокси"
    assert after == [], "доступ вернулся — отметка снята"


def test_new_part_announcement_waits_for_the_read_budget(db):
    """Тип новой части неизвестен и лимит чтений цикла исчерпан — объявление ждёт следующего цикла, а не уходит
    без типа и не тратит чужой бюджет."""
    async def scenario():
        async with session() as s:
            s.add(User(id=86))
            fr = await _known_franchise(s, 8600)
            await _page(s, 8600, "Сага", last=(1, 2), franchise_id=fr.id)
            part = await _page(s, 8601, "Сага: фильм", read=False, franchise_id=fr.id)
            part.content_type = None
            await s.execute(text("INSERT INTO franchise_members (franchise_id, hdrezka_id, announce) "
                                 "VALUES (:f, 8601, 'pending')"), {"f": fr.id})
            await svc.subscribe_franchise(s, 86, fr.id)
            await s.commit()
        from test_integration import film_page
        site = FakeSite("", {8601: film_page(8601, "Сага: фильм", [(8600, "Сага", 2020)])})
        poller = Poller(site)
        poller._reads_left = 0
        first = await poller.announce_new_parts()
        poller._reads_left = 5
        second = await poller.announce_new_parts()
        return first, second, site.reads

    assert db(scenario) == (0, 1, [8601])


# ----------------------------------------------------------------------------- ревью PR (25.09.2026)

def test_broken_new_part_does_not_stop_the_rest_of_the_cycle(db):
    """Неожиданная ошибка при объявлении новой части роняла весь цикл: обновление подписанных страниц, сверка
    франшиз и очередь каталога не выполнялись ни разу, а проверка здоровья молчала."""
    async def scenario():
        async with session() as s:
            s.add(User(id=87))
            fr = await _known_franchise(s, 8700)
            fr.refreshed_at = svc.now()                         # суточная сверка сегодня не нужна
            await _page(s, 8700, "Сага", last=(1, 5), rows=[(1, 5)], franchise_id=fr.id)
            part = await _page(s, 8701, "Сага: фильм", read=False, franchise_id=fr.id)
            part.content_type = None
            await s.execute(text("INSERT INTO franchise_members (franchise_id, hdrezka_id, announce) "
                                 "VALUES (:f, 8701, 'pending')"), {"f": fr.id})
            other = await _page(s, 8800, "Другой сериал", last=(1, 3), rows=[(1, 3)])
            other.page_refreshed_at = svc.now() - timedelta(hours=13)
            await svc.subscribe_page(s, 87, other.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(8800, "Другой сериал", "series", 1, 3, "Дубляж")]}),
                        {8701: RuntimeError("неожиданная ошибка разбора"), 8800: title_page(8800, "Другой сериал", 1, 3, VOICES)})
        await Poller(site).cycle()
        async with session() as s:
            refreshed = await s.scalar(text("SELECT page_refreshed_at > now() - interval '1 minute' FROM pages "
                                            "WHERE hdrezka_id = 8800"))
            member = await s.scalar(text("SELECT announce FROM franchise_members WHERE hdrezka_id = 8701"))
            return (refreshed, await svc.meta_get(s, "refresh_ok_at") is not None,
                    await svc.meta_get(s, "refresh_failed"), member)

    assert db(scenario) == (True, True, "1", "sent"), \
        "вторая часть цикла выполнилась; часть с ошибкой чтения объявлена без типа; ошибка — в refresh_failed"


def test_announcement_error_of_one_part_does_not_stop_the_others(db, monkeypatch):
    """Сбой записи объявления одной части — лог, часть остаётся pending до следующего цикла, остальные уходят."""
    original = svc.enqueue_new_part

    async def flaky(s, franchise_id, part, *args, **kwargs):
        if part.hdrezka_id == 8901:
            raise RuntimeError("сбой записи")
        return await original(s, franchise_id, part, *args, **kwargs)
    monkeypatch.setattr(svc, "enqueue_new_part", flaky)

    async def scenario():
        async with session() as s:
            s.add(User(id=89))
            fr = await _known_franchise(s, 8900)
            await _page(s, 8900, "Сага", last=(1, 2), franchise_id=fr.id)
            for hid in (8901, 8902):
                await _page(s, hid, f"Сага: фильм {hid}", franchise_id=fr.id)
                await s.execute(text("INSERT INTO franchise_members (franchise_id, hdrezka_id, announce) "
                                     "VALUES (:f, :h, 'pending')"), {"f": fr.id, "h": hid})
            await svc.subscribe_franchise(s, 89, fr.id)
            await s.commit()
        queued = await Poller(FakeSite("", {})).announce_new_parts()
        async with session() as s:
            statuses = dict((await s.execute(text("SELECT hdrezka_id, announce FROM franchise_members "
                                                  "WHERE hdrezka_id IN (8901, 8902)"))).all())
        return queued, statuses

    assert db(scenario) == (1, {8901: "pending", 8902: "sent"})


class _FailsOnce(FakeSite):
    """Страница не отвечает один раз (разовый таймаут), потом — нормально."""

    def __init__(self, home, pages, flaky: int, times: int = 1):
        super().__init__(home, pages)
        self._flaky, self._left = flaky, times

    async def title_page(self, url):
        if f"/{self._flaky}-" in url and self._left:
            self._left -= 1
            self.reads.append(self._flaky)
            raise AccessBlocked("таймаут")
        return await super().title_page(url)


def test_premiere_is_not_delayed_by_an_hour_after_one_failed_read(db):
    """Страница нового сезона уже в базе (из блока частей), но не прочитана; первое чтение ради премьеры не
    удалось. Раньше событие ждало часовую паузу очередей обновления, теперь повтор — в следующем цикле."""
    async def scenario():
        async with session() as s:
            s.add(User(id=92))
            fr = await _known_franchise(s, 9310)
            fr.refreshed_at = svc.now()                         # суточная сверка сегодня не нужна
            await _page(s, 9310, "Сага [ТВ-1]", last=(1, 12), rows=[(1, 12)], franchise_id=fr.id, finished=True)
            await _page(s, 9311, "Сага [ТВ-2]", franchise_id=fr.id, read=False)
            await s.execute(text("INSERT INTO franchise_members (franchise_id, hdrezka_id, announce) "
                                 "VALUES (:f, 9311, 'baseline')"), {"f": fr.id})
            await svc.subscribe_franchise(s, 92, fr.id)
            await s.commit()
        site = _FailsOnce(block({TODAY: [(9311, "Сага [ТВ-2]", "series", 2, 1, "Дубляж")]}),
                          {9311: title_page(9311, "Сага [ТВ-2]", 2, 1, VOICES, [(9310, "Сага [ТВ-1]", 2020)])}, 9311)
        poller = Poller(site)

        async def kinds():
            async with session() as s:
                return (await s.execute(text("SELECT kind FROM notifications WHERE user_id = 92"))).scalars().all()

        await poller.cycle()
        first = await kinds()
        await poller.cycle()
        return first, await kinds(), site.reads

    first, second, reads = db(scenario)
    assert first == [] and second == ["episode"], "премьера — со следующим циклом, а не через час"
    assert reads.count(9311) == 2


def test_event_about_unreadable_new_title_goes_on_after_three_failures(db):
    """Тайтла ещё нет в базе, и его страница не читается. Раньше отметка неудачи откатывалась вместе со страницей,
    которую завело событие, — счётчик не рос, и событие откладывалось каждый цикл, пока блок его хранит."""
    async def scenario():
        site = _FailsOnce(block({TODAY: [(9401, "Новый тайтл", "series", 1, 1, "Дубляж")]}),
                          {9401: title_page(9401, "Новый тайтл", 1, 1, VOICES)}, 9401, times=100)
        poller = Poller(site)
        recorded = []
        for _ in range(3):
            await poller.cycle()
            async with session() as s:
                recorded.append(await s.scalar(text("SELECT count(*) FROM episodes e JOIN pages p ON p.id = e.page_id "
                                                    "WHERE p.hdrezka_id = 9401")))
        async with session() as s:
            failures = await s.scalar(text("SELECT read_failures FROM pages WHERE hdrezka_id = 9401"))
        return recorded, failures, site.reads.count(9401)

    assert db(scenario) == ([0, 0, 1], 3, 3), "две неудачи — событие ждёт, третья — идёт без чтения"


def test_page_locks_do_not_outlive_their_event(db, monkeypatch):
    """Чтение страницы ради события блокирует все части франшизы (FOR UPDATE). Раньше весь блок обновлений шёл
    одной транзакцией, и блокировки держались до конца прохода: поиск или ссылка в боте на те же страницы ждали
    до statement_timeout. Теперь каждое событие — своей транзакцией."""
    async def scenario():
        async with session() as s:
            s.add(User(id=95))
            fr = await _known_franchise(s, 9510)
            fr.refreshed_at = svc.now()
            await _page(s, 9510, "Сага [ТВ-1]", last=(1, 12), rows=[(1, 12)], franchise_id=fr.id, finished=True)
            await svc.subscribe_franchise(s, 95, fr.id)
            await _page(s, 9600, "Другой", last=(1, 1), rows=[(1, 1)])
            await s.commit()
        # Блок разбирается от старых событий к новым: сначала премьера ТВ-2 (чтение страницы), потом другой сериал.
        site = FakeSite(block({TODAY: [(9600, "Другой", "series", 1, 2, "Дубляж"),
                                       (9511, "Сага [ТВ-2]", "series", 2, 1, "Дубляж")]}),
                        {9511: title_page(9511, "Сага [ТВ-2]", 2, 1, VOICES, [(9510, "Сага [ТВ-1]", 2020)])})
        locked = []
        original = Poller._apply_update

        async def probe(self, s, item, fresh):
            if item.hdrezka_id == 9600:           # второе событие: держит ли ещё поллер строку части 9510?
                async with session() as other:
                    try:
                        await other.execute(text("SELECT id FROM pages WHERE hdrezka_id = 9510 FOR UPDATE NOWAIT"))
                        locked.append(False)
                    except Exception:
                        locked.append(True)
                    await other.rollback()
            return await original(self, s, item, fresh)
        monkeypatch.setattr(Poller, "_apply_update", probe)
        await Poller(site).cycle()
        async with session() as s:
            kinds = (await s.execute(text("SELECT kind FROM notifications WHERE user_id = 95"))).scalars().all()
        return locked, kinds

    locked, kinds = db(scenario)
    assert locked == [False], "после события с чтением строки частей франшизы свободны"
    assert kinds == ["episode"], "премьера разослана"


# ----------------------------------------------------------------------------- скачки из блока (19.11.2026)

RINGS = "Кольца власти"
LOSTFILM = [(56, "лостфильм (LostFilm)")]


def _seasons_page(hid: int, title: str, seasons: dict[int, int], aired: tuple = ()) -> str:
    """Страница со списком серий нескольких сезонов ({сезон: последняя серия}, озвучка по умолчанию) и расписанием:
    aired — (сезон, серия) со статусом «✓»."""
    last = max(seasons)
    eps = "".join(f'<li class="b-simple_episode__item" data-season_id="{se}" data-episode_id="{e}"></li>'
                  for se, n in seasons.items() for e in range(1, n + 1))
    rows = "".join(f"<tr><td>{se} сезон {e} серия</td><td><b>Серия {e}</b></td><td></td><td>1 ноября 2026</td>"
                   "<td>✓</td></tr>" for se, e in aired)
    return (f'<div class="b-post__title"><h1>{title}</h1></div>'
            f"<script>initCDNSeriesEvents({hid}, 56, {last}, {seasons[last]}, false)</script>"
            '<ul><li class="b-translator__item" data-translator_id="56">лостфильм (LostFilm)</li></ul>'
            f'<ul>{eps}</ul><table class="b-post__schedule_list">{rows}</table>')


async def _process(poller: Poller) -> tuple[int, int]:
    async with session() as s:
        result = await poller.process_updates(s)
        await s.commit()
    return result


async def _episodes(hid: int) -> list[tuple[int, int]]:
    async with session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT e.season, e.episode FROM episodes e JOIN pages p ON p.id = e.page_id "
            "WHERE p.hdrezka_id = :h ORDER BY 1, 2"), {"h": hid})).all()]


def test_false_season_from_the_feed_is_skipped_and_the_real_next_episode_is_new(db, caplog):
    """16.11.2026: блок выдал «Кольца власти» 5×7 при сезонах 2–3 на странице. Серия ушла подписчикам и стала последней
    записанной — настоящая 3×5 после неё записалась молча как старая."""
    async def scenario():
        async with session() as s:
            s.add(User(id=97))
            page = await _page(s, 9850, RINGS, last=(3, 4), rows=[(2, 8)] + [(3, e) for e in range(1, 5)], voices=LOSTFILM)
            await svc.subscribe_page(s, 97, page.id)
            await s.commit()
        false_event = {TODAY - timedelta(days=2): [(9850, RINGS, "series", 5, 7, "LostFilm")]}
        site = FakeSite(block(false_event), {9850: _seasons_page(9850, RINGS, {2: 8, 3: 4})})
        poller = Poller(site)
        first = await _process(poller)
        notes_after_first = await _notifications(97)
        poller.client = site = FakeSite(block({**false_event, TODAY: [(9850, RINGS, "series", 3, 5, "LostFilm")]}),
                                        {9850: _seasons_page(9850, RINGS, {2: 8, 3: 5})})
        second = await _process(poller)
        return first, notes_after_first, second, site.reads, await _episodes(9850), await _notifications(97)

    caplog.set_level(logging.WARNING, logger="poller")
    first, notes_after_first, second, reads, episodes, notes = db(scenario)
    assert first == (0, 0) and notes_after_first == [], "ложной серии нет ни в базе, ни в уведомлениях"
    assert "ПОДОЗРИТЕЛЬНО: Кольца власти s5e7 из ленты — на странице нет, пропускаю" in caplog.text
    assert second == (1, 1) and notes == [("episode", 3, 5)], "настоящая 3×5 — новая, с уведомлением"
    assert reads == [], "3×5 — не скачок, страницу не читаем; 5×7 недавно проверена — тоже"
    assert (5, 7) not in episodes and (3, 5) in episodes


def test_false_event_hanging_in_the_feed_for_days_is_read_rarely(db, monkeypatch, caplog):
    """19.11.2026: ложная 5×7 висит в блоке днями рядом с настоящими 3×1, 3×3, 3×4. Её строку удалили из базы — через
    минуту поллер записал её снова и повторил уведомления. Теперь: ни записи, ни уведомлений; страница перечитывается
    не чаще SUSPECT_RECHECK, после перезапуска поллера — одно чтение."""
    async def scenario():
        async with session() as s:
            s.add(User(id=98))
            page = await _page(s, 9860, RINGS, last=(3, 6), rows=[(3, e) for e in range(1, 7)], voices=LOSTFILM)
            await svc.subscribe_page(s, 98, page.id)
            await s.commit()
        site = FakeSite(block({TODAY - timedelta(days=3): [(9860, RINGS, "series", 5, 7, "LostFilm")],
                               TODAY - timedelta(days=1): [(9860, RINGS, "series", 3, e, "LostFilm") for e in (1, 3, 4)]}),
                        {9860: _seasons_page(9860, RINGS, {2: 8, 3: 6})})
        reads = []
        poller = Poller(site)
        for _ in range(3):
            await poller.cycle()
            reads.append(len(site.reads))
        monkeypatch.setattr(poller_mod, "SUSPECT_RECHECK", timedelta(0))      # интервал прошёл
        await poller.cycle()
        reads.append(len(site.reads))
        monkeypatch.setattr(poller_mod, "SUSPECT_RECHECK", timedelta(hours=6))
        restarted = Poller(site)                                               # перезапуск: память проверок пуста
        for _ in range(2):
            await restarted.cycle()
            reads.append(len(site.reads))
        async with session() as s:
            notes = await s.scalar(text("SELECT count(*) FROM notifications"))
        return reads, await _episodes(9860), notes

    caplog.set_level(logging.WARNING, logger="poller")
    reads, episodes, notes = db(scenario)
    assert reads == [1, 1, 1, 2, 3, 3], "одно чтение на интервал и одно после перезапуска, а не каждый цикл"
    assert episodes == [(3, e) for e in range(1, 7)] and notes == 0
    assert caplog.text.count("ПОДОЗРИТЕЛЬНО: Кольца власти s5e7") == 3, "предупреждение — на каждое чтение, не на цикл"


def test_real_jumps_are_confirmed_by_the_page(db):
    """Настоящий скачок проходит: новый сезон в списке серий страницы; премьера, которой нет в списке (он — озвучки по
    умолчанию), но расписание отмечает её вышедшей; пропуск серии, когда на странице есть обе."""
    async def scenario():
        async with session() as s:
            s.add(User(id=99))
            for hid, last in ((9870, (2, 8)), (9871, (2, 8)), (9872, (1, 5))):
                page = await _page(s, hid, f"Сериал {hid}", last=last, rows=[last], voices=LOSTFILM)
                await svc.subscribe_page(s, 99, page.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(9870, "Сериал 9870", "series", 3, 1, "LostFilm"),
                                       (9871, "Сериал 9871", "series", 3, 1, "LostFilm"),
                                       (9872, "Сериал 9872", "series", 1, 7, "LostFilm")]}),
                        {9870: _seasons_page(9870, "Сериал 9870", {2: 8, 3: 1}),
                         9871: _seasons_page(9871, "Сериал 9871", {2: 8}, aired=[(2, 8), (3, 1)]),
                         9872: _seasons_page(9872, "Сериал 9872", {1: 7})})
        result = await _process(Poller(site))
        return result, sorted(site.reads), await _notifications(99)

    result, reads, notes = db(scenario)
    assert reads == [9870, 9871, 9872], "каждый скачок сверен со страницей"
    assert result == (3, 3) and notes == [("episode", 1, 7), ("episode", 3, 1), ("episode", 3, 1)]


def test_jump_waits_for_the_read_budget_and_for_the_page_then_trusts_the_feed(db, caplog):
    """Лимит чтений цикла исчерпан — скачок ждёт следующего цикла, а не проходит без проверки. Страница не ответила —
    тоже ждёт, как обязательное чтение (19.11.2026: сайт иногда отдаёт 502, и одна неудача чтения пропускала ложное
    событие людям); после READ_GIVE_UP_AFTER неудач подряд — верим блоку: премьеру терять нельзя."""
    async def scenario():
        async with session() as s:
            s.add(User(id=100))
            page = await _page(s, 9880, RINGS, last=(3, 4), rows=[(3, 4)], voices=LOSTFILM)
            await svc.subscribe_page(s, 100, page.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(9880, RINGS, "series", 4, 1, "LostFilm")]}), {9880: AccessBlocked("502")})
        poller = Poller(site)
        poller._reads_left = 0
        waited = await _process(poller), list(site.reads), await _notifications(100)
        steps = []
        for _ in range(poller_mod.READ_GIVE_UP_AFTER):
            caplog.clear()
            await poller.cycle()
            steps.append((len(site.reads), await _notifications(100), "верю ленте" in caplog.text))
        async with session() as s:
            failures = await s.scalar(text("SELECT read_failures FROM pages WHERE hdrezka_id = 9880"))
        return waited, steps, failures

    caplog.set_level(logging.WARNING, logger="poller")
    waited, steps, failures = db(scenario)
    assert waited == ((0, 0), [], []), "без чтения скачок не проходит — ждёт цикла"
    assert steps[:-1] == [(1, [], False), (2, [], False)], "страница не ответила — событие ждёт следующего цикла"
    assert steps[-1] == (3, [("episode", 4, 1)], True), "после трёх неудач подряд — верим ленте"
    assert failures == poller_mod.READ_GIVE_UP_AFTER, "каждая неудача чтения отмечена"
    assert "ПОДОЗРИТЕЛЬНО: Кольца власти s4e1 из ленты, страница не прочиталась — верю ленте" in caplog.text


def test_jump_already_missing_on_the_page_is_skipped_when_the_reread_fails(db, monkeypatch, caplog):
    """Ложная 5×7 уже сверена: на странице её нет. Через SUSPECT_RECHECK страницу перечитываем, а сайт отвечает 502 —
    раньше это была «неудача чтения → верю ленте», и ложная серия уходила людям снова. Доказательство уже есть:
    неудача перечитывания событие не пропускает, сколько бы их ни было (19.11.2026)."""
    async def scenario():
        async with session() as s:
            s.add(User(id=101))
            page = await _page(s, 9890, RINGS, last=(3, 4), rows=[(3, e) for e in range(1, 5)], voices=LOSTFILM)
            await svc.subscribe_page(s, 101, page.id)
            await s.commit()
        home = block({TODAY - timedelta(days=2): [(9890, RINGS, "series", 5, 7, "LostFilm")]})
        poller = Poller(FakeSite(home, {9890: _seasons_page(9890, RINGS, {2: 8, 3: 4})}))
        await poller.cycle()                                                 # сверили: 5×7 на странице нет
        monkeypatch.setattr(poller_mod, "SUSPECT_RECHECK", timedelta(0))      # пора перепроверить
        poller.client = site = FakeSite(home, {9890: AccessBlocked("502")})
        for _ in range(poller_mod.READ_GIVE_UP_AFTER + 2):
            await poller.cycle()
        async with session() as s:
            notes = await s.scalar(text("SELECT count(*) FROM notifications"))
        return len(site.reads), await _episodes(9890), notes

    caplog.set_level(logging.WARNING, logger="poller")
    reads, episodes, notes = db(scenario)
    assert notes == 0 and (5, 7) not in episodes, "ни записи, ни уведомлений"
    assert reads == poller_mod.READ_GIVE_UP_AFTER, "страница перечитывается, пока не наступила пауза после неудач"
    assert "верю ленте" not in caplog.text
    assert "ПОДОЗРИТЕЛЬНО: Кольца власти s5e7 из ленты, страница не прочиталась — на ней уже не было, пропускаю" \
        in caplog.text
