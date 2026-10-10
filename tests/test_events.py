"""Действия людей в боте (app/events.py, 10.10.2026): что пишется, что нет, как читается в /events и чистится."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import text

import app.bot.main as main
from app import events
from app import service as svc
from app.bot import guard
from app.db import session
from app.models import User
from test_bot_handlers import SearchSite, _card_html, _message


def _msg(text_=None, chat="private", content_type="text", uid=7):
    return SimpleNamespace(message=SimpleNamespace(
        text=text_, content_type=content_type, chat=SimpleNamespace(type=chat),
        from_user=SimpleNamespace(id=uid, is_bot=False)), callback_query=None)


def _click(data, chat="private", uid=7):
    return SimpleNamespace(message=None, callback_query=SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid), message=SimpleNamespace(chat=SimpleNamespace(type=chat))))


def test_what_is_recorded_is_the_kind_of_action_not_the_text():
    menu_find = next(iter(main.MENU["btn_find"]))
    assert main._event_of(_msg("/start")) == (7, "cmd", "/start")
    assert main._event_of(_msg("/start p_4100")) == (7, "cmd", "/start ссылка"), "аргумент deep-link не пишем"
    assert main._event_of(_msg("/my@HDRezkaSeriesBot")) == (7, "cmd", "/my")
    assert main._event_of(_msg(menu_find)) == (7, "menu", "btn_find")
    assert main._event_of(_msg("Тайна Коко, мой телефон 123")) == (7, "text", None), "текст сообщения не пишем"
    assert main._event_of(_msg(None, content_type="photo")) == (7, "media", "photo")
    assert main._event_of(_click("sub:4100")) == (7, "button", "sub"), "id в кнопке не пишем"
    assert main._event_of(_click("subf_all:12")) == (7, "button", "subf_all")
    assert main._event_of(_click("что-то старое")) == (7, "button", "stale")
    assert main._event_of(_msg("/start", chat="group")) is None and main._event_of(_click("sub:1", chat="group")) is None


def test_middleware_records_and_the_handler_still_runs(db):
    handled = []

    async def handler(event, data):
        handled.append(event)
        return "ok"

    async def scenario():
        mw = main.RecordEvents()
        r1 = await mw(handler, _msg("/start"), {})
        r2 = await mw(handler, _click("startlang:ru"), {})
        async with session() as s:
            rows = (await s.execute(text("SELECT user_id, kind, detail FROM user_events ORDER BY id"))).all()
        return r1, r2, [tuple(r) for r in rows]

    r1, r2, rows = db(scenario)
    assert r1 == r2 == "ok" and len(handled) == 2
    assert rows == [(7, "cmd", "/start"), (7, "button", "startlang")]


def test_failed_write_does_not_stop_the_answer(monkeypatch):
    """База недоступна — человек всё равно получает ответ; в лог одна строка."""
    def broken():
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(events, "session", broken)
    asyncio.run(events.record(7, "cmd", "/start"))


def test_search_outcome_is_recorded_without_the_query(db, monkeypatch):
    fake = SearchSite(_card_html(4601, "Выходит", "1 сезон, 5 серия"))
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(main, "_background", lambda coro: coro.close())
    monkeypatch.setattr(guard, "site_actions", guard.UserLimiter(per_minute=6, per_hour=60))
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=10, per_hour=120))

    async def scenario():
        await main._handle_search(_message(46), "ru", "секретный запрос 4601")
        fake.search_html = ""
        await main._handle_search(_message(46), "ru", "другой запрос")
        async with session() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT kind, detail FROM user_events WHERE user_id = 46 ORDER BY id"))).all()]

    assert db(scenario) == [("search", "site:1"), ("search", "site:0")]


def test_events_summary_and_path_read_like_a_story(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=55, username="new_<guy>"))
            await s.commit()
        for kind, detail in [("cmd", "/start"), ("button", "startlang"), ("menu", "btn_find"), ("text", None),
                             ("search", "site:0"), ("search", "catalog:3"), ("link", "gone")]:
            await events.record(55, kind, detail)
        async with session() as s:
            return await events.summary(s, tz=3), await events.path(s, 55, tz=3), await events.path(s, 99, tz=3)

    summary, path, empty = db(scenario)
    assert "@new_&lt;guy&gt;" in summary and "7 действий" in summary, "имя экранировано: ответ идёт в HTML"
    assert ("/start → кнопка «язык при старте» → меню «🔍 Найти» → текст → поиск: ничего (сайт) → "
            "поиск: нашлось 3 (каталог) → ссылка: страницы нет на сайте") in summary
    assert path.count("\n") >= 8 and "поиск: ничего (сайт)" in path
    assert "Действий за 30 дней нет" in empty


def test_old_events_are_purged(db):
    async def scenario():
        await events.record(1, "cmd", "/start")
        async with session() as s:
            await s.execute(text("INSERT INTO user_events (user_id, kind, at) VALUES (2, 'cmd', CAST(:at AS timestamptz))"),
                            {"at": svc.now() - timedelta(days=events.KEEP_DAYS + 1)})
            n = await events.purge(s)
            await s.commit()
            left = (await s.execute(text("SELECT user_id FROM user_events"))).scalars().all()
        return n, left

    assert db(scenario) == (1, [1])
