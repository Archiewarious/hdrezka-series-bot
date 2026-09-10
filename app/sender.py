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
    UPDATE notifications n SET status = 'sending', attempts = n.attempts + 1
      FROM claimed c WHERE n.id = c.id
    RETURNING n.id, n.user_id, n.kind, n.ref_id
""")

EPISODE_SQL = text("""
    SELECT p.id, p.hdrezka_id, p.title, p.url, p.poster_url, p.poster_file_id,
           p.default_translator, p.franchise_id, e.season, e.episode
      FROM episodes e JOIN pages p ON p.id = e.page_id WHERE e.id = :id""")
FIRST_VOICE_SQL = text("""
    SELECT translator_id FROM episode_voices WHERE episode_id = :id ORDER BY seen_at LIMIT 1""")
VOICE_NAME_SQL = text("""
    SELECT v.name FROM voices v JOIN episodes e ON e.page_id = v.page_id
     WHERE e.id = :id AND v.translator_id = :tid LIMIT 1""")
PART_SQL = text("""
    SELECT p.id, p.hdrezka_id, p.title, p.url, p.poster_url, p.poster_file_id,
           p.content_type, p.year, p.is_finished, p.franchise_id, f.name
      FROM pages p LEFT JOIN franchises f ON f.id = p.franchise_id WHERE p.id = :id""")
SUB_SQL = text("""
    SELECT id, scope FROM subscriptions
     WHERE user_id = :u AND (page_id = :p OR franchise_id = :f)
     ORDER BY (scope = 'page') DESC LIMIT 1""")


@dataclass
class Rendered:
    kind: str
    page_id: int
    hdrezka_id: int
    title: str                 # без экранирования — для кнопок
    text: str                  # HTML одиночного сообщения / подписи к фото
    line: str                  # HTML-строка дайджеста
    watch_label: str           # «▶ Смотреть 4×22» / «▶ Открыть»
    watch_url: str
    poster_url: str | None
    poster_file_id: str | None
    sub_id: int | None
    sub_scope: str | None      # 'page' | 'franchise'
    can_follow: bool = False   # new_part: показывать «➕ Следить за этой частью»


def watch_url(url: str, translator: int | None, season: int, episode: int) -> str:
    """F18: хвост #t:-s:-e: открывает в плеере нужную озвучку и серию."""
    return f"{url}#t:{translator}-s:{season}-e:{episode}" if translator is not None else url


def fit_button(prefix: str, title: str, suffix: str = "", limit: int = BUTTON_MAX_LEN) -> str:
    """Текст кнопки ≤ limit символов: режем название, суффикс (S×E) сохраняем."""
    room = limit - len(prefix) - len(suffix)
    if len(title) > room:
        title = title[: max(room - 1, 0)] + "…"
    return f"{prefix}{title}{suffix}"


async def _sub_for(s, user_id: int, page_id: int, franchise_id: int | None) -> tuple[int | None, str | None]:
    row = (await s.execute(SUB_SQL, {"u": user_id, "p": page_id, "f": franchise_id or -1})).first()
    return (row[0], row[1]) if row else (None, None)


async def _render(s, user_id: int, kind: str, ref_id: int, lang: str = "ru") -> Rendered | None:
    if kind == "episode" or kind.startswith("voice:"):
        row = (await s.execute(EPISODE_SQL, {"id": ref_id})).first()
        if not row:
            return None
        page_id, hid, title, url, poster_url, file_id, default_tid, franchise_id, season, episode = row
        title = title[:TITLE_MAX_LEN]
        esc_title = html.escape(title)
        sxe = f"{season}×{episode}"
        body = t(lang, "n_episode_body", s=season, e=episode)
        voice = None
        if kind.startswith("voice:"):
            tid = int(kind.split(":")[1])
            name = await s.scalar(VOICE_NAME_SQL, {"id": ref_id, "tid": tid})
            voice = html.escape(name or f"#{tid}")
            body += t(lang, "n_voice_suffix", v=voice)
        else:
            # Озвучка «любая»: ведём в ту, где серия появилась первой, и подписываем её в тексте;
            # неизвестна — в озвучку по умолчанию, без подписи.
            tid = await s.scalar(FIRST_VOICE_SQL, {"id": ref_id})
            if tid is not None:
                name = await s.scalar(VOICE_NAME_SQL, {"id": ref_id, "tid": tid})
                if name:
                    voice = html.escape(name)
                    body += t(lang, "n_voice_suffix", v=voice)
            else:
                tid = default_tid
        sub_id, scope = await _sub_for(s, user_id, page_id, franchise_id)
        return Rendered(
            kind=kind, page_id=page_id, hdrezka_id=hid, title=title,
            text=f"🎬 <b>{esc_title}</b>\n{body}",
            line=f"• <b>{esc_title}</b> — {sxe}" + (f" ({voice})" if voice else ""),
            watch_label=t(lang, "btn_watch", sxe=sxe), watch_url=watch_url(url, tid, season, episode),
            poster_url=poster_url, poster_file_id=file_id, sub_id=sub_id, sub_scope=scope,
        )
    if kind == "new_part":
        row = (await s.execute(PART_SQL, {"id": ref_id})).first()
        if not row:
            return None
        page_id, hid, title, url, poster_url, file_id, ctype, year, finished, franchise_id, fname = row
        title = title[:TITLE_MAX_LEN]
        esc_title, esc_fname = html.escape(title), html.escape(fname or "")
        what = t(lang, "kind_film") if ctype == "film" else (t(lang, "kind_series") if ctype == "series" else None)
        tail = " · ".join(x for x in (what, year) if x)
        sub_id, scope = await _sub_for(s, user_id, page_id, franchise_id)
        return Rendered(
            kind=kind, page_id=page_id, hdrezka_id=hid, title=title,
            text=t(lang, "n_new_part", f=esc_fname, t=esc_title) + (f" · {tail}" if tail else ""),
            line=t(lang, "n_new_part_line", f=esc_fname, t=esc_title) + (f" · {tail}" if tail else ""),
            watch_label=t(lang, "btn_open"), watch_url=url,
            poster_url=poster_url, poster_file_id=file_id, sub_id=sub_id, sub_scope=scope,
            can_follow=ctype != "film" and not finished,
        )
    return None


def _unfollow_button(r: Rendered, lang: str = "ru") -> InlineKeyboardButton:
    label = t(lang, "btn_unfollow_fr" if r.sub_scope == "franchise" else "btn_unfollow")
    return InlineKeyboardButton(text=label, callback_data=f"unsubq:{r.sub_id}")


def _single_keyboard(r: Rendered, lang: str = "ru") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=r.watch_label, url=r.watch_url)]]
    if r.kind == "new_part":
        if r.can_follow:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_follow_part"), callback_data=f"sub:{r.hdrezka_id}")])
    elif r.sub_id:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_other_voices"), callback_data=f"voices:{r.sub_id}")])
    if r.sub_id:
        rows.append([_unfollow_button(r, lang)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _digest(items: list[Rendered], lang: str = "ru") -> tuple[str, InlineKeyboardMarkup]:
    head = t(lang, "digest_episodes" if any(r.kind != "new_part" for r in items) else "digest_parts")
    body = head + "\n\n" + "\n".join(r.line for r in items)
    if len(body) > TG_MAX_LEN:
        body = body[:TG_MAX_LEN - 1] + "…"
    rows = []
    for r in items[:DIGEST_MAX_BUTTONS]:
        suffix = "" if r.kind == "new_part" else " " + r.watch_label.split()[-1]   # S×E
        rows.append([InlineKeyboardButton(text=fit_button("▶ ", r.title, suffix), url=r.watch_url)])
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_photo(bot: Bot, s, user_id: int, r: Rendered, kb: InlineKeyboardMarkup) -> bool:
    """True — ушло фото. False — постера нет или Telegram его не принял: отправим текстом."""
    msg = await posters.send_photo_cached(bot, s, user_id, r.page_id, r.poster_url, r.poster_file_id, r.text, kb)
    return msg is not None


async def _deliver(bot: Bot, s, user_id: int, items: list[Rendered], photos: bool, lang: str) -> None:
    if len(items) == 1:
        r = items[0]
        kb = _single_keyboard(r, lang)
        if photos and await _send_photo(bot, s, user_id, r, kb):
            return
        await bot.send_message(user_id, r.text, reply_markup=kb, disable_web_page_preview=True)
        return
    body, kb = _digest(items, lang)
    await bot.send_message(user_id, body, reply_markup=kb, disable_web_page_preview=True)


async def send_batch(bot: Bot, limiter: RateLimiter) -> int:
    async with session() as s:
        rows = (await s.execute(CLAIM_SQL, {"limit": cfg.send_batch, "max_attempts": MAX_ATTEMPTS})).all()
        await s.commit()
        if not rows:
            return 0

        by_user: dict[int, list] = defaultdict(list)
        for nid, uid, kind, ref in rows:
            by_user[uid].append((nid, kind, ref))

        sent_total = 0
        for user_id, items in by_user.items():
            prefs = (await s.execute(text("SELECT photos, lang FROM users WHERE id = :u"), {"u": user_id})).first()
            photos, lang = (prefs[0], prefs[1] or "ru") if prefs else (True, "ru")
            rendered = [await _render(s, user_id, k, r, lang) for _, k, r in items]
            rendered = [r for r in rendered if r]
            ids = [nid for nid, _, _ in items]
            if not rendered:
                await _mark(s, ids, "failed", "nothing to render")
                continue

            await limiter.acquire()
            try:
                await _deliver(bot, s, user_id, rendered, photos is not False, lang)
                await _mark(s, ids, "sent")
                sent_total += len(ids)
            except TelegramRetryAfter as exc:
                log.warning("Flood control от Telegram: пауза %s с", exc.retry_after)
                await _requeue(s, ids, seconds=exc.retry_after)
                await s.commit()
                await asyncio.sleep(exc.retry_after)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — больше не пишем и не копим очередь.
                await s.execute(text("UPDATE users SET is_active = false, blocked_at = now() WHERE id = :u"), {"u": user_id})
                await s.execute(text("UPDATE notifications SET status = 'failed', error = 'bot blocked' "
                                     "WHERE user_id = :u AND status IN ('pending', 'sending')"), {"u": user_id})
            except TelegramBadRequest as exc:
                await _mark(s, ids, "failed", str(exc)[:400])
            except Exception as exc:
                log.exception("Не отправилось пользователю %s", user_id)
                await _requeue(s, ids, seconds=60, error=str(exc)[:400])
        await s.commit()
        return sent_total


async def _mark(s, ids: list[int], status: str, error: str | None = None) -> None:
    await s.execute(text("UPDATE notifications SET status = CAST(:st AS varchar), error = CAST(:e AS text), "
                         "sent_at = CASE WHEN CAST(:st AS varchar) = 'sent' THEN now() END "
                         "WHERE id = ANY(CAST(:ids AS bigint[]))"),
                    {"st": status, "e": error, "ids": ids})


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
