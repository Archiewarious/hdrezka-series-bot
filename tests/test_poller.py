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
        async with session() as s:
            first = await poller.announce_new_parts(s)
            await s.commit()
        poller._reads_left = 5
        async with session() as s:
            second = await poller.announce_new_parts(s)
            await s.commit()
        return first, second, site.reads

    assert db(scenario) == (0, 1, [8601])
