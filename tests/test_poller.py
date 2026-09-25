"""Поллер: очередь каталога, сверка франшиз, границы транзакций цикла, паузы при потере доступа.
Сайт подменён (FakeSite из test_integration)."""
import asyncio
from datetime import timedelta

from sqlalchemy import text

from app import lifecycle
from app import service as svc
from app.db import session
from app.models import Franchise, Subscription, User
from app.poller import Poller
from app.rezka.client import AccessBlocked
from test_integration import TODAY, FakeSite, _known_franchise, _page, block, title_page

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
