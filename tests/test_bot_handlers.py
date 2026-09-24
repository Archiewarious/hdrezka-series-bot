"""Обработчики бота на подставных Message и CallbackQuery: ни одного запроса в Telegram и на сайт."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.bot.main as main
from app import service as svc
from app.bot import guard
from app.db import session
from app.models import Subscription, User
from test_integration import FakeSite, _page, title_page


class FakeMessage:
    """Сообщение бота в чате человека: запоминает ответы и правки."""

    def __init__(self, chat_id: int, reply_markup=None):
        self.chat = SimpleNamespace(id=chat_id)
        self.bot = SimpleNamespace()
        self.photo = None
        self.reply_markup = reply_markup
        self.answers, self.edits = [], []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        note = FakeMessage(self.chat.id, reply_markup)
        self.notes = getattr(self, "notes", []) + [note]
        return note

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.reply_markup = reply_markup

    async def delete(self):
        pass


class FakeCallback:
    def __init__(self, user_id: int, data: str, message: FakeMessage | None = None):
        self.from_user = SimpleNamespace(id=user_id, username=None, language_code="ru")
        self.data = data
        self.message = message or FakeMessage(user_id)
        self.toasts = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.toasts.append(text)


@pytest.fixture
def site(monkeypatch):
    """Клиент бота — подменный сайт; лимиты — свежие на каждый тест."""
    fake = FakeSite("", {})
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(guard, "site_actions", guard.UserLimiter(per_minute=6, per_hour=60))
    monkeypatch.setattr(guard, "cheap_actions", guard.UserLimiter(per_minute=30, per_hour=600))
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=10, per_hour=120))
    return fake


@pytest.mark.parametrize("exhausted", [True, False])
def test_subscription_does_not_read_the_site_when_the_limit_is_spent(db, site, monkeypatch, exhausted):
    """24.09.2026: подписка читала страницу с сайта под дешёвым лимитом — ~300 запросов в час с одного аккаунта.
    Теперь при исчерпанном общем потолке подписка создаётся без запроса, страницу прочитает поллер."""
    if exhausted:
        monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=0, per_hour=0))
    site._pages[4100] = title_page(4100, "Сериал", 1, 3, [(56, "Дубляж", None)])

    async def scenario():
        async with session() as s:
            s.add(User(id=41))
            await _page(s, 4100, "Сериал", last=(1, 3), read=False)
            await s.commit()
        cb = FakeCallback(41, "sub:4100")
        await main.cb_subscribe(cb)
        async with session() as s:
            subs = (await s.execute(select(Subscription.user_id))).scalars().all()
        return subs, site.reads, cb.message.answers

    subs, reads, answers = db(scenario)
    assert subs == [41] and answers, "подписка создана, карточка отправлена"
    assert reads == ([] if exhausted else [4100])


def test_bot_wide_site_ceiling(monkeypatch):
    """Общий потолок на всех: сверх него — «сайт занят», хотя у каждого человека свой лимит не исчерпан."""
    monkeypatch.setattr(guard, "site_actions", guard.UserLimiter(per_minute=6, per_hour=60))
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=3, per_hour=120))
    assert [guard.site_permit(uid) for uid in (1, 2, 3, 4, 5)] == [None, None, None, "busy", "busy"]
    assert [guard.site_permit(9) for _ in range(7)][-1] in ("too_fast", "busy")


def test_link_to_unknown_page_answers_busy_without_request(db, site, monkeypatch):
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=0, per_hour=0))

    async def scenario():
        msg = FakeMessage(42)
        msg.from_user = SimpleNamespace(id=42, username=None, language_code="ru")
        await main._handle_link(msg, "ru", 4200, "/series/x/4200-a.html")
        return msg.answers, site.reads

    answers, reads = db(scenario)
    assert reads == [] and answers[0][0] == main.t("ru", "busy")


# ----------------------------------------------------------------------------- шаг 8 аудита (24.09.2026)

from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.models import Page  # noqa: E402


def _card_html(hid: int, title: str, info: str, section: str = "series") -> str:
    return (f'<div class="b-content__inline_item" data-id="{hid}" data-url="https://rezka.test/{section}/x/{hid}-a.html">'
            f'<div class="b-content__inline_item-cover"><span class="cat {section}"></span><span class="info">{info}</span></div>'
            f'<div class="b-content__inline_item-link"><a href="#">{title}</a><div>2026, Япония, Аниме</div></div></div>')


class SearchSite(FakeSite):
    def __init__(self, search_html: str = "", pages=None):
        super().__init__("", pages or {})
        self.search_html, self.searches = search_html, []

    async def search(self, query: str) -> str:
        self.searches.append(query)
        return self.search_html


@pytest.fixture
def no_background(monkeypatch):
    """Фоновые задачи бота (уточнение «завершён») в этих тестах не нужны — и не должны пережить тест."""
    monkeypatch.setattr(main, "_background", lambda coro: coro.close())


def _message(uid: int) -> FakeMessage:
    msg = FakeMessage(uid)
    msg.from_user = SimpleNamespace(id=uid, username=None, language_code="ru")
    return msg


def test_link_to_a_known_page_is_answered_from_the_database(db, site, no_background):
    async def scenario():
        async with session() as s:
            page = await _page(s, 4300, "Известный", last=(1, 3))
            page.last_event_at = svc.now()
            await s.commit()
        msg = _message(43)
        await main._handle_link(msg, "ru", 4300, "/series/x/4300-a.html")
        return site.reads, msg.answers

    reads, answers = db(scenario)
    assert reads == [] and "Известный" in answers[0][0]


def test_link_to_an_unknown_page_is_read_from_the_site(db, site, no_background):
    site._pages[4400] = title_page(4400, "С сайта", 1, 2, [(56, "Дубляж", None)])

    async def scenario():
        msg = _message(44)
        await main._handle_link(msg, "ru", 4400, "/series/x/4400-a.html")
        async with session() as s:
            url = await s.scalar(select(Page.url).where(Page.hdrezka_id == 4400))
        return site.reads, [a[0] for a in msg.answers], url

    reads, answers, url = db(scenario)
    assert reads == [4400] and any("С сайта" in a for a in answers) and url == "/series/x/4400-a.html"


def test_link_timeout_answers_busy(db, site, no_background):
    site._pages[4500] = asyncio.TimeoutError()

    async def scenario():
        msg = _message(45)
        await main._handle_link(msg, "ru", 4500, "/series/x/4500-a.html")
        return msg.answers[0][0], msg.notes[0].edits[-1][0], site.reads

    first, final, reads = db(scenario)
    assert first == main.t("ru", "reading_page") and final == main.t("ru", "busy") and reads == [4500]


def test_site_search_shows_airing_titles(db, monkeypatch, no_background):
    fake = SearchSite(_card_html(4601, "Выходит", "1 сезон, 5 серия") + _card_html(4602, "Фильм", "", "films"))
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(guard, "site_actions", guard.UserLimiter(per_minute=6, per_hour=60))
    monkeypatch.setattr(guard, "bot_site", guard.UserLimiter(per_minute=10, per_hour=120))

    async def scenario():
        msg = _message(46)
        await main._handle_search(msg, "ru", "уникальный запрос 4601")
        return fake.searches, msg

    searches, msg = db(scenario)
    assert searches == ["уникальный запрос 4601"] and msg.answers[0][0] == main.t("ru", "searching")
    text_, kb = msg.notes[0].edits[-1]
    buttons = [b.text for row in kb.inline_keyboard for b in row]
    assert text_ == main.t("ru", "airing_now_n", n=1) and buttons[0].startswith("➕ Выходит"), "фильм не предлагается"


def test_subscription_limits_and_films(db, site):
    async def scenario():
        async with session() as s:
            s.add_all([User(id=47), User(id=48)])
            await s.flush()
            await s.execute(text("INSERT INTO pages (hdrezka_id, title, url) SELECT 470000 + g, 'x', '/x/' || g || '-a.html' "
                                 "FROM generate_series(1, 100) g"))
            await s.execute(text("INSERT INTO subscriptions (user_id, scope, page_id) "
                                 "SELECT 47, 'page', id FROM pages WHERE hdrezka_id > 470000"))
            await _page(s, 4700, "Ещё один", last=(1, 2))
            film = await _page(s, 4800, "Фильм")
            film.content_type = "film"
            await s.commit()
        full, film_cb = FakeCallback(47, "sub:4700"), FakeCallback(48, "sub:4800")
        await main.cb_subscribe(full)
        await main.cb_subscribe(film_cb)
        async with session() as s:
            counts = dict((await s.execute(text("SELECT user_id, count(*) FROM subscriptions GROUP BY 1"))).all())
        return full.toasts, film_cb.toasts, counts

    full, film, counts = db(scenario)
    assert full == [main.t("ru", "max_subs", n=guard.MAX_SUBSCRIPTIONS)] and counts == {47: 100}
    assert film == [main.t("ru", "cannot_sub")], "на фильм подписаться нельзя"


def test_time_zone_stays_within_bounds(db, site):
    async def scenario():
        async with session() as s:
            s.add_all([User(id=49, tz_offset=14), User(id=50, tz_offset=-12)])
            await s.commit()
        await main.cb_settings(FakeCallback(49, "set:tz:1"))
        await main.cb_settings(FakeCallback(50, "set:tz:-1"))
        async with session() as s:
            return dict((await s.execute(text("SELECT id, tz_offset FROM users WHERE id IN (49, 50)"))).all())

    assert db(scenario) == {49: 14, 50: -12}


def test_turning_quiet_hours_on_reschedules_the_queue(db, site):
    """Настройки доставки действуют и на уже стоящие в очереди уведомления."""
    utc_hour = datetime.now(timezone.utc).hour
    tz = -utc_hour if utc_hour <= 12 else 24 - utc_hour          # у человека сейчас около полуночи

    async def scenario():
        async with session() as s:
            s.add(User(id=51, tz_offset=tz, quiet_from=None, quiet_to=None))
            await s.flush()
            await s.execute(text("INSERT INTO notifications (user_id, kind, ref_id) VALUES (51, 'episode', 1)"))
            await s.commit()
        await main.cb_settings(FakeCallback(51, "set:quiet:1"))
        async with session() as s:
            return await s.scalar(text("SELECT next_attempt_at > now() + interval '7 hours' FROM notifications"))

    assert db(scenario) is True, "в тихие часы — до утра"


def test_cannot_unsubscribe_someone_else(db, site):
    async def scenario():
        async with session() as s:
            s.add_all([User(id=61), User(id=62)])
            page = await _page(s, 6100, "Чужое", last=(1, 2))
            await svc.subscribe_page(s, 61, page.id)
            await s.commit()
            sub_id = await s.scalar(select(Subscription.id))
        cb = FakeCallback(62, f"unsub:{sub_id}")
        await main.cb_unsubscribe(cb)
        async with session() as s:
            left = (await s.execute(select(Subscription.user_id))).scalars().all()
        return cb.toasts, left

    toasts, left = db(scenario)
    assert toasts == [main.t("ru", "toast_no_sub")] and left == [61]


# ----------------------------------------------------------------------------- шаг 9 аудита (24.09.2026)

def test_season_inside_a_followed_franchise_is_not_subscribed_twice(db, site):
    from app.models import Franchise

    async def scenario():
        async with session() as s:
            s.add(User(id=64))
            fr = Franchise(key_hdrezka_id=6400, name="Сага")
            s.add(fr)
            await s.flush()
            await _page(s, 6400, "Сага [ТВ-2]", last=(2, 3), franchise_id=fr.id)
            await svc.subscribe_franchise(s, 64, fr.id)
            await s.commit()
        cb = FakeCallback(64, "sub:6400")
        await main.cb_subscribe(cb)
        async with session() as s:
            scopes = (await s.execute(select(Subscription.scope))).scalars().all()
        return cb.toasts, scopes, cb.message.answers

    toasts, scopes, answers = db(scenario)
    assert toasts == [main.t("ru", "already_franchise")] and scopes == ["franchise"]
    assert "Сага" in answers[0][0], "показана карточка франшизы"


def test_today_is_the_persons_date_not_the_servers(db):
    async def scenario():
        async with session() as s:
            s.add_all([User(id=65, tz_offset=14), User(id=66, tz_offset=-12)])
            await s.commit()
        return await main._today(65), await main._today(66)

    east, west = db(scenario)
    now = datetime.now(timezone.utc)
    assert east == (now + timedelta(hours=14)).date() and west == (now - timedelta(hours=12)).date()
    rows = [("Сериал", 1, 2, east)]
    assert main.t("ru", "today") in main.calendar_text("ru", rows, today=east)
    assert main.t("ru", "tomorrow") in main.calendar_text("ru", rows, today=east - timedelta(days=1))


def test_link_button_without_address_is_not_shown():
    kb = main._kb([[("Сайт", main.Url("")), ("Расписание", "sched:p:1")], [("Пусто", main.Url(""))]])
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [["Расписание"]]
    kb = main._kb([[("Сайт", main.Url("https://rezka.test/a/1-x.html"))], [("Кнопка", "/a/1-x.html")]])
    assert kb.inline_keyboard[0][0].url == "https://rezka.test/a/1-x.html"
    assert kb.inline_keyboard[1][0].callback_data == "/a/1-x.html", "строка без Url — кнопка, а не ссылка"
