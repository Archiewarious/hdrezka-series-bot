"""Обратная связь: письмо автору и ответы через «Ответить» — на настоящем Postgres, Telegram подменён."""
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import text

from app import feedback
from app.db import session
from app.models import User

ADMIN, PERSON = 500, 700
PERSON_TG = SimpleNamespace(id=PERSON, first_name="Катя", last_name=None, username="kate")


class Tg:
    """Подставной Telegram: запоминает отправленное, номера сообщений идут подряд."""

    def __init__(self, blocked=()):
        self.out, self.blocked, self._n = [], set(blocked), 100

    def _put(self, kind, chat_id, body, reply_parameters):
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
        self._n += 1
        self.out.append((kind, chat_id, self._n, body, reply_parameters.message_id if reply_parameters else None))
        return SimpleNamespace(message_id=self._n)

    async def send_message(self, chat_id, text_, reply_parameters=None, **kw):
        return self._put("send", chat_id, text_, reply_parameters)

    async def copy_message(self, chat_id, from_chat_id, message_id, reply_parameters=None, **kw):
        return self._put("copy", chat_id, (from_chat_id, message_id), reply_parameters)


async def _people(topic="bug"):
    async with session() as s:
        s.add_all([User(id=ADMIN), User(id=PERSON, username="kate", lang="uk")])
        await s.flush()
        await feedback.begin(s, PERSON, topic)
        await s.commit()


def test_letter_reaches_author_as_card_and_copy(db, monkeypatch):
    monkeypatch.setattr(feedback, "cfg", SimpleNamespace(admin_ids=[ADMIN]))
    tg = Tg()

    async def scenario():
        await _people()
        async with session() as s:
            before = await feedback.pending_topic(s, PERSON)
            fb_id = await feedback.submit(tg, s, PERSON_TG, before, PERSON, 42)
            await s.commit()
        async with session() as s:
            return before, fb_id, await feedback.pending_topic(s, PERSON)

    before, fb_id, after = db(scenario)
    assert (before, fb_id, after) == ("bug", 1, None), "тема ждала письма, после отправки — нет"
    (k1, chat1, card_id, card, _), (k2, chat2, _, source, reply_to) = tg.out
    assert (k1, chat1, k2, chat2) == ("send", ADMIN, "copy", ADMIN)
    assert source == (PERSON, 42) and reply_to == card_id, "копия письма — ответом на карточку"
    assert "🐞 Ошибка" in card and "обращение №1" in card and "@kate" in card and f"<code>{PERSON}</code>" in card
    assert "🌐 uk · подписок: 0" in card


def test_answer_and_follow_up_find_their_way(db, monkeypatch):
    monkeypatch.setattr(feedback, "cfg", SimpleNamespace(admin_ids=[ADMIN]))
    tg = Tg()

    async def scenario():
        await _people("idea")
        async with session() as s:
            await feedback.submit(tg, s, PERSON_TG, "idea", PERSON, 42)
            await s.commit()
        card_id = tg.out[0][2]
        async with session() as s:
            assert await feedback.find(s, PERSON, card_id) is None, "у человека в чате такой карточки нет"
            fb = await feedback.find(s, ADMIN, card_id)
            note = await feedback.answer(tg, s, fb, ADMIN, 77, "Спасибо, <b>добавлю</b>")
            await s.commit()
        answer_id = tg.out[-1][2]
        async with session() as s:
            fb2 = await feedback.find(s, PERSON, answer_id)
            ok = await feedback.follow_up(tg, s, fb2, PERSON_TG, PERSON, 43)
            await s.commit()
            answered = await s.scalar(text("SELECT answered_at IS NOT NULL FROM feedback WHERE id = 1"))
        return fb, note, fb2, ok, answered

    fb, note, fb2, ok, answered = db(scenario)
    assert fb.side == "admin" and note == "✅ Доставлено" and answered
    kind, chat, _, body, _ = tg.out[2]
    assert (kind, chat) == ("send", PERSON)
    assert body.startswith("✉️ <b>Відповідь автора</b>") and "<b>добавлю</b>" in body, "ответ — на языке человека"
    assert fb2.side == "user" and fb2.id == fb.id and ok
    (k1, chat1, card2, more, _), (k2, chat2, _, source, reply_to) = tg.out[3:]
    assert (k1, chat1, k2, chat2) == ("send", ADMIN, "copy", ADMIN)
    assert "обращение №1</b>, продолжение" in more and source == (PERSON, 43) and reply_to == card2


def test_answer_to_person_who_blocked_the_bot(db, monkeypatch):
    monkeypatch.setattr(feedback, "cfg", SimpleNamespace(admin_ids=[ADMIN]))
    tg = Tg()

    async def scenario():
        await _people()
        async with session() as s:
            await feedback.submit(tg, s, PERSON_TG, "bug", PERSON, 42)
            await s.commit()
        tg.blocked.add(PERSON)
        async with session() as s:
            fb = await feedback.find(s, ADMIN, tg.out[0][2])
            note = await feedback.answer(tg, s, fb, ADMIN, 77, "ок")
            await s.commit()
            active = await s.scalar(text("SELECT is_active FROM users WHERE id = :u"), {"u": PERSON})
        return note, active

    assert db(scenario) == ("❌ Не доставлено: человек заблокировал бота", False)


def test_nobody_reachable_means_not_sent(db, monkeypatch):
    """Автор недоступен — человеку «не получилось», а не ложное «отправлено»; тема остаётся выбранной."""
    monkeypatch.setattr(feedback, "cfg", SimpleNamespace(admin_ids=[ADMIN]))
    tg = Tg(blocked={ADMIN})

    async def scenario():
        await _people()
        async with session() as s:
            fb_id = await feedback.submit(tg, s, PERSON_TG, "bug", PERSON, 42)
        async with session() as s:                       # вызывающий не коммитит при None
            return fb_id, await feedback.pending_topic(s, PERSON)

    assert db(scenario) == (None, "bug")


def test_pending_topic_expires(db):
    async def scenario():
        await _people()
        async with session() as s:
            await s.execute(text("UPDATE users SET feedback_until = now() - interval '1 minute' WHERE id = :u"),
                            {"u": PERSON})
            await s.commit()
            return await feedback.pending_topic(s, PERSON)

    assert db(scenario) is None, "через 15 минут текст снова поиск"
