"""Рассылка. Отдельный процесс: очередь в Postgres (FOR UPDATE SKIP LOCKED),
token bucket под лимит Telegram (~30 сообщений/сек всем, 1/сек в один чат).

События одного пользователя, лежащие в очереди одновременно, склеиваются в одно
сообщение — так соблюдается лимит «1 сообщение в секунду в чат» и человек не
получает пять уведомлений подряд.

Формат (docs/PRODUCT_AND_SCALE.md §6, §7.8): одно событие → фото + подпись + кнопки;
несколько → текстовый дайджест с кнопками «▶» внизу. Постер качаем напрямую с CDN
(он не забанен, туннель не тратим), первый раз грузим байтами, Telegram отдаёт
file_id → pages.poster_file_id; дальше все уведомления по сериалу идут по нему.
Кнопка «▶ Смотреть» ведёт на серию и озвучку: url#t:{translator}-s:{s}-e:{e}.

Здесь же: очистка отправленного. Здоровье потока событий — app/health.py.
Правило: технические сообщения в Telegram не отправляются никогда.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text

from app import posters
from app.config import cfg
from app.db import init_db, session
from app.i18n import t

log = logging.getLogger("sender")

MAX_ATTEMPTS = 5
PURGE_AFTER_DAYS = 7
TG_MAX_LEN = 4000
BUTTON_MAX_LEN = 40
DIGEST_MAX_BUTTONS = 10
TITLE_MAX_LEN = 150
STUCK_MINUTES = 10
PER_CHAT_GAP = 1.1                      # Telegram: не чаще сообщения в секунду в один чат


class RateLimiter:
    """Token bucket: не быстрее `rate` сообщений в секунду."""

    def __init__(self, rate: float) -> None:
        self._rate, self._allowance, self._last = rate, rate, time.monotonic()

    async def acquire(self) -> None:
        now = time.monotonic()
        self._allowance = min(self._rate, self._allowance + (now - self._last) * self._rate)
        self._last = now
        if self._allowance < 1:
            await asyncio.sleep((1 - self._allowance) / self._rate)
            self._allowance = 0
        else:
            self._allowance -= 1


CLAIM_SQL = text("""
    WITH claimed AS (
        SELECT id FROM notifications
         WHERE status = 'pending' AND next_attempt_at <= now() AND attempts < :max_attempts
         ORDER BY user_id, id
         LIMIT :limit
         FOR UPDATE SKIP LOCKED)
    UPDATE notifications n SET status = 'sending', attempts = n.attempts + 1, next_attempt_at = now()
      FROM claimed c WHERE n.id = c.id
    RETURNING n.id, n.user_id, n.kind, n.ref_id
""")
# Процесс упал между захватом и отметкой — уведомление осталось «отправляется» и не ушло бы никогда.
# Время захвата лежит в next_attempt_at: зависшие дольше STUCK_MINUTES возвращаются в очередь.
RECOVER_SQL = text("""
    UPDATE notifications SET status = 'pending'
     WHERE status = 'sending' AND next_attempt_at < now() - make_interval(mins => CAST(:mins AS int))
""")

EPISODE_SQL = text("""
    SELECT p.id, p.title, p.url, p.poster_url, p.poster_file_id, p.default_translator, e.season, e.episode
      FROM episodes e JOIN pages p ON p.id = e.page_id WHERE e.id = :id""")
FIRST_VOICE_SQL = text("""
    SELECT translator_id FROM episode_voices WHERE episode_id = :id ORDER BY seen_at LIMIT 1""")
VOICE_NAME_SQL = text("""
    SELECT v.name FROM voices v JOIN episodes e ON e.page_id = v.page_id
     WHERE e.id = :id AND v.translator_id = :tid LIMIT 1""")
PART_SQL = text("""
    SELECT p.id, p.title, p.url, p.poster_url, p.poster_file_id, p.content_type, p.year, f.name
      FROM pages p LEFT JOIN franchises f ON f.id = p.franchise_id WHERE p.id = :id""")


@dataclass
class Rendered:
    kind: str
    page_id: int
    title: str                 # без экранирования — для кнопки дайджеста
    text: str                  # HTML поста: подпись к фото или отдельное сообщение
    line: str                  # HTML-блок этого события в дайджесте
    watch_url: str
    poster_url: str | None
    poster_file_id: str | None


def watch_url(url: str, translator: int | None, season: int, episode: int) -> str:
    """F18: хвост #t:-s:-e: открывает в плеере нужную озвучку и серию."""
    return f"{url}#t:{translator}-s:{season}-e:{episode}" if translator is not None else url


def fit_button(prefix: str, title: str, suffix: str = "", limit: int = BUTTON_MAX_LEN) -> str:
    """Текст кнопки ≤ limit символов: режем название, суффикс сохраняем."""
    room = limit - len(prefix) - len(suffix)
    if len(title) > room:
        title = title[: max(room - 1, 0)] + "…"
    return f"{prefix}{title}{suffix}"


async def _render(s, user_id: int, kind: str, ref_id: int, lang: str = "ru") -> Rendered | None:
    """Уведомление как пост (11.09.2026): название, пустая строка, сезон и серия, озвучка."""
    if kind == "episode" or kind.startswith("voice:"):
        row = (await s.execute(EPISODE_SQL, {"id": ref_id})).first()
        if not row:
            return None
        page_id, title, url, poster_url, file_id, default_tid, season, episode = row
        voice = None
        if kind.startswith("voice:"):
            tid = int(kind.split(":")[1])
            voice = await s.scalar(VOICE_NAME_SQL, {"id": ref_id, "tid": tid}) or f"#{tid}"
        else:
            # «Любая» озвучка: ведём в ту, где серия появилась первой, и подписываем её;
            # неизвестна — в озвучку по умолчанию, без подписи.
            tid = await s.scalar(FIRST_VOICE_SQL, {"id": ref_id})
            if tid is not None:
                voice = await s.scalar(VOICE_NAME_SQL, {"id": ref_id, "tid": tid})
            else:
                tid = default_tid
        title = title[:TITLE_MAX_LEN]
        head = f"🎬 <b>{html.escape(title)}</b>"
        ep = t(lang, "n_season_episode", s=season, e=episode)
        dub = t(lang, "n_voice", v=html.escape(voice)) if voice else ""
        return Rendered(
            kind=kind, page_id=page_id, title=title,
            text=f"{head}\n\n{ep}" + (f"\n{dub}" if dub else ""),
            line=f"{head}\n{ep}" + (f" · {dub}" if dub else ""),
            watch_url=watch_url(url, tid, season, episode), poster_url=poster_url, poster_file_id=file_id,
        )
    if kind == "new_part":
        row = (await s.execute(PART_SQL, {"id": ref_id})).first()
        if not row:
            return None
        page_id, title, url, poster_url, file_id, ctype, year, fname = row
        title = title[:TITLE_MAX_LEN]
        what = t(lang, "kind_film") if ctype == "film" else (t(lang, "kind_series") if ctype == "series" else None)
        tail = " · ".join(x for x in (what, year) if x)
        head = t(lang, "n_new_part", f=html.escape(fname or title))
        name = f"🎬 <b>{html.escape(title)}</b>"
        return Rendered(
            kind=kind, page_id=page_id, title=title,
            text=f"{head}\n\n{name}" + (f"\n🎞 {tail}" if tail else ""),
            line=f"{head}\n{name}" + (f" · {tail}" if tail else ""),
            watch_url=url, poster_url=poster_url, poster_file_id=file_id,
        )
    return None


def _single_keyboard(r: Rendered, lang: str = "ru") -> InlineKeyboardMarkup:
    """Пост с одной кнопкой (решение 11.09.2026). Озвучки и отписка — в «📋 Мои подписки»;
    нажатия на кнопки старых уведомлений бот по-прежнему обрабатывает."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "btn_watch_site"), url=r.watch_url)]])


def _digest(items: list[Rendered], lang: str = "ru") -> tuple[str, InlineKeyboardMarkup]:
    """Несколько событий одним сообщением. Текст режется только между событиями — обрезка посреди HTML-тега
    ломает разметку, и Telegram отклоняет сообщение. Кнопка — одна на сериал: две озвучки одной серии или
    две серии подряд ведут к последнему событию."""
    head = t(lang, "digest_episodes" if any(r.kind != "new_part" for r in items) else "digest_parts")
    parts, size = [head], len(head)
    for r in items:
        if size + len(r.line) + 2 > TG_MAX_LEN - 3:
            parts.append("…")
            break
        parts.append(r.line)
        size += len(r.line) + 2
    buttons: dict[int, tuple[str, str]] = {}
    for r in items:
        buttons[r.page_id] = (r.title, r.watch_url)
    rows = [[InlineKeyboardButton(text=fit_button("▶ ", title), url=url)]
            for title, url in list(buttons.values())[:DIGEST_MAX_BUTTONS]]
    return "\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows)


async def _deliver(bot: Bot, s, user_id: int, items: list[Rendered], photos: bool, lang: str) -> int:
    """Отправляет пост или дайджест и возвращает номер сообщения Telegram — он сохраняется у уведомления."""
    if len(items) == 1:
        r = items[0]
        kb = _single_keyboard(r, lang)
        if photos:
            msg = await posters.send_photo_cached(bot, s, user_id, r.page_id, r.poster_url, r.poster_file_id, r.text, kb)
            if msg is not None:
                return msg.message_id
        return (await bot.send_message(user_id, r.text, reply_markup=kb, disable_web_page_preview=True)).message_id
    body, kb = _digest(items, lang)
    return (await bot.send_message(user_id, body, reply_markup=kb, disable_web_page_preview=True)).message_id


async def send_batch(bot: Bot, limiter: RateLimiter) -> int:
    """Каждое событие — отдельный пост (11.09.2026): склейка двух серий в текстовое сообщение без постера
    выглядела как «уведомления не было». Одним сообщением — только в режиме «дайджест раз в день».
    Отметка «отправлено» — сразу после каждого поста, с коммитом: сбой посреди рассылки не дублирует ушедшее."""
    async with session() as s:
        await s.execute(RECOVER_SQL, {"mins": STUCK_MINUTES})
        rows = (await s.execute(CLAIM_SQL, {"limit": cfg.send_batch, "max_attempts": MAX_ATTEMPTS})).all()
        await s.commit()
        if not rows:
            return 0

        by_user: dict[int, list] = defaultdict(list)
        for nid, uid, kind, ref in rows:
            by_user[uid].append((nid, kind, ref))

        sent_total = 0
        for user_id, items in by_user.items():
            prefs = (await s.execute(text("SELECT photos, lang, digest_hour FROM users WHERE id = :u"),
                                     {"u": user_id})).first()
            photos, lang, digest = ((prefs[0] is not False, prefs[1] or "ru", prefs[2] is not None)
                                    if prefs else (True, "ru", False))
            pairs = [(nid, await _render(s, user_id, k, r, lang)) for nid, k, r in items]
            dead = [nid for nid, r in pairs if r is None]
            if dead:
                await _mark(s, dead, "failed", "nothing to render")
            pairs = [(nid, r) for nid, r in pairs if r is not None]
            messages = [pairs] if digest and len(pairs) > 1 else [[pair] for pair in pairs]

            for n, message in enumerate(messages):
                ids = [nid for nid, _ in message]
                if n:
                    await asyncio.sleep(PER_CHAT_GAP)
                await limiter.acquire()
                try:
                    message_id = await _deliver(bot, s, user_id, [r for _, r in message], photos, lang)
                    await _mark(s, ids, "sent", message_id=message_id)
                    sent_total += len(ids)
                    log.info("Доставлено пользователю %s: сообщение Telegram №%s, уведомления %s", user_id, message_id, ids)
                except TelegramRetryAfter as exc:
                    rest = [nid for m in messages[n:] for nid, _ in m]
                    log.warning("Flood control от Telegram: пауза %s с", exc.retry_after)
                    await _requeue(s, rest, seconds=exc.retry_after)
                    await s.commit()
                    await asyncio.sleep(exc.retry_after)
                    break
                except TelegramForbiddenError:
                    # Пользователь заблокировал бота — больше не пишем и не копим очередь.
                    await s.execute(text("UPDATE users SET is_active = false, blocked_at = now() WHERE id = :u"), {"u": user_id})
                    await s.execute(text("UPDATE notifications SET status = 'failed', error = 'bot blocked' "
                                         "WHERE user_id = :u AND status IN ('pending', 'sending')"), {"u": user_id})
                    await s.commit()
                    break
                except TelegramBadRequest as exc:
                    await _mark(s, ids, "failed", str(exc)[:400])
                except Exception as exc:
                    log.exception("Не отправилось пользователю %s", user_id)
                    await _requeue(s, ids, seconds=60, error=str(exc)[:400])
                await s.commit()
        await s.commit()
        return sent_total


async def _mark(s, ids: list[int], status: str, error: str | None = None, message_id: int | None = None) -> None:
    await s.execute(text("UPDATE notifications SET status = CAST(:st AS varchar), error = CAST(:e AS text), "
                         "sent_at = CASE WHEN CAST(:st AS varchar) = 'sent' THEN now() END, "
                         "tg_message_id = CAST(:m AS bigint) "
                         "WHERE id = ANY(CAST(:ids AS bigint[]))"),
                    {"st": status, "e": error, "ids": ids, "m": message_id})


async def _requeue(s, ids: list[int], seconds: int, error: str | None = None) -> None:
    await s.execute(text("UPDATE notifications SET status = 'pending', error = CAST(:e AS text), "
                         "next_attempt_at = now() + make_interval(secs => CAST(:sec AS double precision)) "
                         "WHERE id = ANY(CAST(:ids AS bigint[]))"),
                    {"e": error, "sec": float(seconds), "ids": ids})


async def purge(s) -> int:
    r = await s.execute(text("DELETE FROM notifications WHERE status IN ('sent', 'failed') "
                             "AND coalesce(sent_at, created_at) < now() - make_interval(days => :d)"),
                        {"d": PURGE_AFTER_DAYS})
    return r.rowcount or 0


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    limiter = RateLimiter(cfg.send_rate)
    log.info("Sender запущен: %s сообщений/сек", cfg.send_rate)
    last_purge = 0.0
    try:
        while True:
            sent = await send_batch(bot, limiter)
            if sent:
                log.info("Отправлено уведомлений: %s", sent)
            else:
                await asyncio.sleep(5)
            now = time.monotonic()
            if now - last_purge > 3600:
                last_purge = now
                async with session() as s:
                    n = await purge(s)
                    await s.commit()
                if n:
                    log.info("Очистка: удалено %s отправленных уведомлений", n)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
