"""Процессы поллера и отправщика (шаг 5 аудита, 24.09.2026): сбой одного человека, остановка по сигналу,
сторож зависаний, один отправщик на базу, отметка «отправлено» отдельно от отправки."""
import asyncio
import time

from sqlalchemy import text

from app import lifecycle, sender
from app.db import session
from test_integration import FakeBot, _two_new_episodes


def _statuses():
    async def q():
        async with session() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT user_id, status, attempts, next_attempt_at <= now() + interval '1 second' FROM notifications "
                "ORDER BY id"))).all()]
    return q()


def test_render_error_of_one_person_does_not_stop_the_others(db, monkeypatch):
    """Раньше исключение в _render роняло весь проход — и ни один человек в пачке ничего не получал."""
    real = sender._render

    async def broken_for_21(s, user_id, kind, ref, lang):
        if user_id == 21:
            raise RuntimeError("битые данные")
        return await real(s, user_id, kind, ref, lang)
    monkeypatch.setattr(sender, "_render", broken_for_21)
    monkeypatch.setattr(sender, "PER_CHAT_GAP", 0)

    async def scenario():
        await _two_new_episodes(21)
        await _two_new_episodes(22)
        bot = FakeBot()
        sent = await sender.send_batch(bot, sender.RateLimiter(1000))
        return sent, [chat for chat, _, _ in bot.sent], await _statuses()

    sent, chats, rows = db(scenario)
    assert sent == 2 and chats == [22, 22], "второй человек получил своё"
    assert [(u, st) for u, st, _, _ in rows if u == 21] == [(21, "pending"), (21, "pending")], "первый — в очереди"


def test_stop_signal_mid_batch_returns_the_rest_to_the_queue(db, monkeypatch):
    """SIGTERM посреди пачки: текущее сообщение досылается, остальные взятые — сразу в pending, без штрафа."""
    monkeypatch.setattr(sender, "PER_CHAT_GAP", 0)

    class StoppingBot(FakeBot):
        async def send_message(self, *a, **kw):
            msg = await super().send_message(*a, **kw)
            lifecycle.request_stop()                     # сигнал пришёл во время первой отправки
            return msg

    async def scenario():
        await _two_new_episodes(23)
        await _two_new_episodes(24)
        bot = StoppingBot()
        sent = await sender.send_batch(bot, sender.RateLimiter(1000))
        return sent, len(bot.sent), await _statuses()

    sent, messages, rows = db(scenario)
    assert sent == 1 and messages == 1
    assert rows[0][1] == "sent"
    assert [(st, att, due) for _, st, att, due in rows[1:]] == [("pending", 0, True)] * 3, \
        "не отправленные — в очереди сразу, попытка не засчитана"


def test_database_failure_after_send_does_not_resend(db, monkeypatch):
    """Сообщение ушло, а отметка «отправлено» не записалась: раньше уведомление возвращалось в очередь
    и уходило второй раз. Теперь номер сообщения — в лог, отметка дописывается следующим проходом."""
    monkeypatch.setattr(sender, "PER_CHAT_GAP", 0)
    real_mark = sender._mark
    broken = {"on": True}

    async def flaky_mark(s, ids, status, error=None, message_id=None):
        if status == "sent" and broken["on"]:
            raise ConnectionError("база недоступна")
        return await real_mark(s, ids, status, error, message_id)
    monkeypatch.setattr(sender, "_mark", flaky_mark)

    async def fast_sleep(sec):
        pass
    monkeypatch.setattr(sender.asyncio, "sleep", fast_sleep)

    async def scenario():
        async with session() as s:
            pass
        await _two_new_episodes(25)
        async with session() as s:
            await s.execute(text("DELETE FROM notifications WHERE id = (SELECT max(id) FROM notifications)"))
            await s.commit()
        bot = FakeBot()
        await sender.send_batch(bot, sender.RateLimiter(1000))
        after_failure = (await _statuses())[0][1], dict(sender._unmarked)
        broken["on"] = False
        async with session() as s:                      # пусть прошло больше 10 минут: RECOVER их не вернёт
            await s.execute(text("UPDATE notifications SET next_attempt_at = now() - interval '1 hour'"))
            await s.commit()
        await sender.send_batch(bot, sender.RateLimiter(1000), recover=True)
        async with session() as s:
            final = tuple((await s.execute(text("SELECT status, tg_message_id FROM notifications"))).one())
        return after_failure, len(bot.sent), final

    (status_then, unmarked), messages, final = db(scenario)
    assert status_then == "sending" and list(unmarked.values()) == [(25, 1001)]
    assert messages == 1 and final == ("sent", 1001), "одно сообщение, отметка дописана позже"


def test_watchdog_exits_a_hung_process():
    exits = []
    dog = lifecycle.Watchdog("тест", 0.2, exit_fn=exits.append, check_every=0.05).start()
    time.sleep(0.5)
    dog.stop()
    assert exits == [1]


def test_watchdog_stays_quiet_while_beaten():
    exits = []
    dog = lifecycle.Watchdog("тест", 0.3, exit_fn=exits.append, check_every=0.05).start()
    for _ in range(10):
        dog.beat()
        time.sleep(0.05)
    dog.stop()
    assert exits == []


def test_second_sender_does_not_get_the_lock(db):
    async def scenario():
        first = await lifecycle.acquire_lock(lifecycle.SENDER_LOCK, "отправщик")
        second = await lifecycle.acquire_lock(lifecycle.SENDER_LOCK, "отправщик", wait=False)
        other = await lifecycle.acquire_lock(lifecycle.POLLER_LOCK, "поллер", wait=False)
        await first.close()
        await other.close()
        return second

    assert db(scenario) is None


def test_sender_loop_survives_failures_with_growing_pause(db, monkeypatch):
    calls, pauses = [], []

    async def failing_batch(bot, limiter, recover=True, beat=None):
        calls.append(1)
        if len(calls) >= 3:
            lifecycle.request_stop()
        raise ConnectionError("база недоступна")

    async def record_pause(seconds, beat=None):
        pauses.append(seconds)
    monkeypatch.setattr(sender, "send_batch", failing_batch)
    monkeypatch.setattr(lifecycle, "pause", record_pause)
    asyncio.run(sender.run(FakeBot(), sender.RateLimiter(1000)))
    assert len(calls) == 3 and pauses == [5, 10, 20], "процесс не упал, пауза растёт"


def test_stop_interrupts_a_long_pause():
    async def scenario():
        t0 = time.monotonic()
        asyncio.get_running_loop().call_later(0.2, lifecycle.request_stop)
        await lifecycle.pause(600)
        return time.monotonic() - t0
    assert asyncio.run(scenario()) < 2


def test_monitoring_ping_only_when_healthy(db, monkeypatch):
    """Адрес один на оба процесса: пинг только при чистом здоровье, иначе живой процесс маскировал бы умерший."""
    import dataclasses
    from app import health
    from app.config import cfg

    pinged = []

    class FakeHttp:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, url):
            pinged.append(url)

            class Resp:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    pass

                async def read(self):
                    return b""
            return Resp()

    monkeypatch.setattr(lifecycle.aiohttp, "ClientSession", FakeHttp)
    problems = [["поллер не завершал цикл уже 30 мин"]]

    async def fake_check(s, at=None, part="all"):
        return problems[0]
    monkeypatch.setattr(health, "check", fake_check)

    async def scenario():
        async with session() as s:
            off = await lifecycle.ping_if_healthy(s)                 # адрес пуст — выключено
            monkeypatch.setattr(lifecycle, "cfg", dataclasses.replace(cfg, healthcheck_ping_url="https://hc.test/abc"))
            monkeypatch.setattr(lifecycle, "_last_ping", 0.0)
            sick = await lifecycle.ping_if_healthy(s)
            monkeypatch.setattr(lifecycle, "_last_ping", 0.0)
            problems[0] = []
            ok = await lifecycle.ping_if_healthy(s)
            throttled = await lifecycle.ping_if_healthy(s)          # сразу второй — не чаще раза в минуту
        return off, sick, ok, throttled

    assert db(scenario) == (False, False, True, False) and pinged == ["https://hc.test/abc"]


def test_poller_finishes_the_current_page_and_stops(db):
    """SIGTERM во время обновления страниц: текущая страница дописывается, следующая не запрашивается."""
    from app import service as svc
    from app.models import User
    from app.poller import Poller
    from datetime import timedelta
    from test_integration import FakeSite, _page, title_page

    class StopOnRead(FakeSite):
        async def title_page(self, url):
            lifecycle.request_stop()
            return await super().title_page(url)

    async def scenario():
        async with session() as s:
            s.add(User(id=26))
            for hid in (2601, 2602):
                page = await _page(s, hid, f"Сериал {hid}", last=(1, 2), rows=[(1, 2)])
                page.page_refreshed_at = svc.now() - timedelta(days=1)
                await svc.subscribe_page(s, 26, page.id)
            await s.commit()
        site = StopOnRead("", {h: title_page(h, f"Сериал {h}", 1, 2, [(56, "Дубляж", None)]) for h in (2601, 2602)})
        done = await Poller(site).refresh_pages()
        async with session() as s:
            fresh = (await s.execute(text("SELECT count(*) FROM pages WHERE page_refreshed_at > now() - interval '1 minute'"))).scalar()
        return done, len(site.reads), fresh

    assert db(scenario) == (1, 1, 1)


def test_healthcheck_process_runs_on_its_own_engine(db):
    """Healthcheck контейнера — отдельный процесс раз в 5 минут: своё соединение без пула (NullPool)."""
    from app import health

    async def scenario():
        from app import service as svc
        async with session() as s:
            for key in ("last_poll_ok", "updates_ok_at"):
                await svc.meta_set(s, key, svc.now().isoformat())
            await s.execute(text("INSERT INTO users (id) VALUES (1)"))
            await s.execute(text("INSERT INTO pages (hdrezka_id, title, url) VALUES (1, 'x', '/x/1-a.html')"))
            await s.execute(text("INSERT INTO episodes (page_id, season, episode) VALUES (1, 1, 1)"))
            await s.commit()

    db(scenario)
    assert asyncio.run(health._main("all")) == 0
