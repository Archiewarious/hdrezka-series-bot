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
from test_integration import _page


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


def test_search_outcome_is_recorded_with_the_query(db, monkeypatch):
    """19.11.2026: новичок четыре раза подряд получил «ничего», а что он вводил — неизвестно. Запрос — в строке итога."""
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
                "SELECT kind, detail, query FROM user_events WHERE user_id = 46 ORDER BY id"))).all()]

    assert db(scenario) == [("search", "site:1", "секретный запрос 4601"), ("search", "site:0", "другой запрос")]


def test_what_was_typed_is_kept_next_to_the_outcome(db, monkeypatch):
    """19.11.2026: короткий запрос, поиск по каталогу, ссылка — что ввёл человек, лежит в строке итога; текст ссылки —
    целиком, с адресом сайта (ссылка с чужого сайта тоже видна)."""
    monkeypatch.setattr(main, "_background", lambda coro: coro.close())
    monkeypatch.setattr(guard, "cheap_actions", guard.UserLimiter(per_minute=30, per_hour=600))

    async def scenario():
        async with session() as s:
            await _page(s, 4700, "Известный сериал", last=(1, 3))
            await s.commit()
        for said in ("я", "Известный  сериал", "вот https://rezka.test/series/y/4700-p.html"):
            msg = _message(47)
            msg.text = said
            await main.on_text(msg)
        async with session() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT kind, detail, query FROM user_events WHERE user_id = 47 ORDER BY id"))).all()]

    assert db(scenario) == [("search", "short", "я"), ("search", "catalog:1", "Известный сериал"),
                            ("link", "known", "вот https://rezka.test/series/y/4700-p.html")]


def test_only_search_and_link_keep_the_text(db):
    """19.11.2026: письмо автору и прочее — без текста, даже если его передали; запрос чистится от управляющих
    символов и режется до длины колонки."""
    async def scenario():
        await events.record(48, "feedback", "bug", "текст письма автору")
        await events.record(48, "text", None, "привет")
        await events.record(48, "search", "site:0", "ab‮cd\x00 \n ef" + "x" * 300)
        await events.record(48, "search", "short", "   ")
        async with session() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT kind, query FROM user_events WHERE user_id = 48 ORDER BY id"))).all()]

    assert db(scenario) == [("feedback", None), ("text", None), ("search", "abcd ef" + "x" * 193), ("search", None)]


def test_events_show_what_was_typed(db):
    """19.11.2026: /events — запрос рядом с итогом; в сводке до 40 символов, в пути целиком; разметка экранирована
    (ответ идёт в HTML). Нажатие на «✓» — своё имя кнопки."""
    long_q = "очень длинный запрос про сериал которого нет нигде вообще"

    async def scenario():
        await events.record(56, "search", "catalog:1", "дом дракона")
        await events.record(56, "search", "site:0", "<b>ведьмак</b> & co")
        await events.record(56, "search", "site:0", long_q)
        await events.record(56, "link", "gone", "https://rezka.test/series/x/1-a.html")
        await events.record(56, "button", "done")
        async with session() as s:
            return await events.summary(s, tz=3), await events.path(s, 56, tz=3)

    summary, path = db(scenario)
    for out in (summary, path):
        assert "поиск «дом дракона»: нашлось 1 (каталог)" in out
        assert "поиск «&lt;b&gt;ведьмак&lt;/b&gt; &amp; co»: ничего (сайт)" in out
        assert "кнопка «✓ (уже слежу)»" in out
    assert f"поиск «{long_q[:39]}…»: ничего (сайт)" in summary and long_q not in summary
    assert f"поиск «{long_q}»: ничего (сайт)" in path
    assert "ссылка «https://rezka.test/series/x/1-a.html»: страницы нет на сайте" in path
    assert main._event_of(_click("done:go:p:183")) == (7, "button", "done")


def test_events_fit_in_one_telegram_message(db):
    """19.11.2026: с длинными запросами сводка и путь всё равно уходят одним сообщением: в сводке нет дальних по
    времени людей (сказано сколько), в пути — самых старых действий; последнее на месте."""
    async def scenario():
        for uid in range(100, 100 + events.SUMMARY_USERS):
            for i in range(events.SUMMARY_TAIL):
                await events.record(uid, "search", "site:0", f"запрос {uid}-{i} " + "я" * 150)
        for i in range(events.PATH_LIMIT):
            await events.record(200, "link", "gone", f"https://rezka.test/{i}/" + "a" * 180)
        async with session() as s:
            return await events.summary(s, tz=0), await events.path(s, 200, tz=0)

    summary, path = db(scenario)
    assert events._size(summary) <= guard.TEXT_MAX and events._size(path) <= guard.TEXT_MAX
    assert "не влезли в сообщение" in summary and summary.endswith("Путь одного человека: /events &lt;id&gt;")
    assert "<code>200</code>" in summary, "самый свежий — первым"
    assert f"https://rezka.test/{events.PATH_LIMIT - 1}/" in path and "https://rezka.test/0/" not in path


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
