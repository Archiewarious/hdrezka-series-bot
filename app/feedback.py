"""Обратная связь (13.09.2026): человек пишет автору через бота, автор отвечает «Ответить» на карточку.

Автор — cfg.admin_ids. Каждому приходит карточка (тема, кто, язык, подписки) и следом копия сообщения
ответом на неё — так доходят и скриншоты, и сообщения людей, скрывших аккаунт при пересылке.
Текст обращений не храним: он у автора в Telegram. В базе — кто, тема и какие сообщения Telegram
относятся к обращению; по ним «Ответить» находит адресата в обе стороны.
"""
from __future__ import annotations

import html
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ReplyParameters
from sqlalchemy import text

from app.config import cfg
from app.i18n import t

log = logging.getLogger("feedback")

TOPICS = ("bug", "idea", "collab", "other")
PENDING_MINUTES = 15            # выбрал тему — столько ждём само сообщение, дальше текст снова поиск


async def begin(s, user_id: int, topic: str) -> None:
    await s.execute(text("""
        UPDATE users SET feedback_topic = :t, feedback_until = now() + make_interval(mins => CAST(:m AS int))
         WHERE id = :u"""), {"t": topic, "m": PENDING_MINUTES, "u": user_id})


async def cancel(s, user_id: int) -> None:
    await s.execute(text("UPDATE users SET feedback_topic = NULL, feedback_until = NULL "
                         "WHERE id = :u AND feedback_topic IS NOT NULL"), {"u": user_id})


async def pending_topic(s, user_id: int) -> str | None:
    return await s.scalar(text("SELECT feedback_topic FROM users WHERE id = :u AND feedback_until > now()"),
                          {"u": user_id})


async def find(s, chat_id: int, message_id: int):
    """Обращение, к которому относится сообщение в этом чате. Номер сообщения уникален только внутри
    чата, поэтому ищем по паре: человек не может «ответить» на карточку, лежащую у автора."""
    return (await s.execute(text("""
        SELECT f.id, f.user_id, f.topic, l.side FROM feedback_links l JOIN feedback f ON f.id = l.feedback_id
         WHERE l.chat_id = :c AND l.message_id = :m"""), {"c": chat_id, "m": message_id})).first()


async def submit(bot, s, user, topic: str, chat_id: int, message_id: int) -> int | None:
    """Новое обращение. None — ни одному автору не доставилось: вызывающий не коммитит (тема остаётся
    выбранной) и просит повторить позже."""
    fb_id = await s.scalar(text("INSERT INTO feedback (user_id, topic) VALUES (:u, :t) RETURNING id"),
                           {"u": user.id, "t": topic})
    info = (await s.execute(text("""
        SELECT lang, created_at, (SELECT count(*) FROM subscriptions WHERE user_id = :u)
          FROM users WHERE id = :u"""), {"u": user.id})).first()
    delivered = 0
    for admin in cfg.admin_ids:
        lang = await _lang_of(s, admin)
        card = t(lang, "fb_card", topic=t(lang, f"fb_topic_{topic}"), n=fb_id, who=_who(user),
                 ulang=info[0], subs=info[2], since=info[1].strftime("%d.%m.%Y"))
        delivered += await _to_admin(bot, s, admin, fb_id, card, chat_id, message_id)
    await cancel(s, user.id)
    return fb_id if delivered else None


async def follow_up(bot, s, fb, user, chat_id: int, message_id: int) -> bool:
    """Человек ответил на ответ автора — продолжение того же обращения."""
    delivered = 0
    for admin in cfg.admin_ids:
        lang = await _lang_of(s, admin)
        card = t(lang, "fb_card_more", topic=t(lang, f"fb_topic_{fb.topic}"), n=fb.id, who=_who(user))
        delivered += await _to_admin(bot, s, admin, fb.id, card, chat_id, message_id)
    return bool(delivered)


async def answer(bot, s, fb, admin_chat: int, message_id: int, html_text: str | None) -> str:
    """Ответ автора → человеку от имени бота. Возвращает подтверждение для автора."""
    lang, admin_lang = await _lang_of(s, fb.user_id), await _lang_of(s, admin_chat)
    head, hint = t(lang, "fb_answer_head"), t(lang, "fb_answer_hint")
    try:
        if html_text is not None:
            sent = [await bot.send_message(fb.user_id, f"{head}\n\n{html_text}\n\n{hint}", disable_web_page_preview=True)]
        else:                                           # фото, файл, голосовое — копией после заголовка
            first = await bot.send_message(fb.user_id, f"{head}\n{hint}")
            sent = [first, await bot.copy_message(fb.user_id, admin_chat, message_id)]
    except TelegramForbiddenError:
        await s.execute(text("UPDATE users SET is_active = false, blocked_at = now() WHERE id = :u"), {"u": fb.user_id})
        return t(admin_lang, "fb_blocked")
    except TelegramBadRequest as exc:
        return t(admin_lang, "fb_not_delivered", reason=html.escape(exc.message))
    await _link(s, fb.id, fb.user_id, "user", *(m.message_id for m in sent))
    await s.execute(text("UPDATE feedback SET answered_at = now() WHERE id = :f"), {"f": fb.id})
    return t(admin_lang, "fb_delivered")


async def _to_admin(bot, s, admin: int, fb_id: int, card: str, chat_id: int, message_id: int) -> bool:
    try:
        head = await bot.send_message(admin, card, disable_web_page_preview=True)
        copy = await bot.copy_message(admin, chat_id, message_id,
                                      reply_parameters=ReplyParameters(message_id=head.message_id,
                                                                       allow_sending_without_reply=True))
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.error("Обращение №%s не доставлено автору %s: %s", fb_id, admin, exc)
        return False
    await _link(s, fb_id, admin, "admin", head.message_id, copy.message_id)
    return True


async def _link(s, fb_id: int, chat_id: int, side: str, *message_ids: int) -> None:
    for mid in message_ids:
        await s.execute(text("""
            INSERT INTO feedback_links (chat_id, message_id, feedback_id, side) VALUES (:c, :m, :f, :side)
            ON CONFLICT DO NOTHING"""), {"c": chat_id, "m": mid, "f": fb_id, "side": side})


async def _lang_of(s, user_id: int) -> str:
    return await s.scalar(text("SELECT lang FROM users WHERE id = :u"), {"u": user_id}) or "ru"


def _who(user) -> str:
    name = html.escape(" ".join(x for x in (user.first_name, user.last_name) if x) or "—")
    at = f" · @{html.escape(user.username)}" if user.username else ""
    return f'<a href="tg://user?id={user.id}">{name}</a>{at} · <code>{user.id}</code>'
