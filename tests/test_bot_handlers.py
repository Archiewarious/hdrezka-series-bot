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
        return FakeMessage(self.chat.id, reply_markup)

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
