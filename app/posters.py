"""Постеры в Telegram (docs/PRODUCT_AND_SCALE.md §6). Скачиваем напрямую с CDN — он не забанен,
туннель не тратим. Первый раз грузим байтами, Telegram отдаёт file_id → pages.poster_file_id;
дальше все сообщения по этому сериалу (уведомления sender'а и карточки бота) идут по file_id:
одна загрузка на сериал, ничего не хранится на диске."""
from __future__ import annotations

import logging

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import text

from app.config import cfg

log = logging.getLogger("posters")

CAPTION_MAX_LEN = 1024
POSTER_TIMEOUT = 10
POSTER_MAX_BYTES = 10 * 1024 * 1024  # лимит Telegram на загрузку фото


async def download(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POSTER_TIMEOUT)) as http:
            async with http.get(url, headers={"User-Agent": cfg.user_agent}) as resp:
                if resp.status != 200 or not resp.content_type.startswith("image/"):
                    log.warning("Постер %s: HTTP %s, %s", url, resp.status, resp.content_type)
                    return None
                data = await resp.read()
                if len(data) > POSTER_MAX_BYTES:
                    log.warning("Постер %s: %s байт — больше лимита Telegram", url, len(data))
                    return None
                return data
    except Exception as exc:  # сеть/таймаут — вызывающий отправит текстом
        log.warning("Постер %s не скачался: %s", url, exc)
        return None


async def send_photo_cached(bot: Bot, s, chat_id: int, page_id: int, poster_url: str | None,
                            poster_file_id: str | None, caption: str,
                            kb: InlineKeyboardMarkup | None) -> Message | None:
    """Фото с подписью. None — постера нет или Telegram его не принял: вызывающий шлёт текст.
    `s` — открытая сессия БД: сюда пишем/сбрасываем file_id (коммитит вызывающий)."""
    caption = caption[:CAPTION_MAX_LEN]
    if poster_file_id:
        try:
            return await bot.send_photo(chat_id, poster_file_id, caption=caption, reply_markup=kb)
        except TelegramBadRequest as exc:
            log.warning("Постер страницы %s: file_id не принят (%s) — загружу заново", page_id, exc.message)
            await s.execute(text("UPDATE pages SET poster_file_id = NULL WHERE id = :p"), {"p": page_id})
    if not poster_url:
        return None
    data = await download(poster_url)
    if not data:
        return None
    try:
        msg = await bot.send_photo(chat_id, BufferedInputFile(data, filename="poster.jpg"),
                                   caption=caption, reply_markup=kb)
    except TelegramBadRequest as exc:
        log.warning("Постер страницы %s: Telegram не принял картинку (%s) — отправлю текстом", page_id, exc.message)
        return None
    if msg.photo:
        # Привязываем к URL: если постер к этому моменту сменился, file_id уже не тот.
        await s.execute(text("UPDATE pages SET poster_file_id = :f WHERE id = :p AND poster_url = :u"),
                        {"f": msg.photo[-1].file_id, "p": page_id, "u": poster_url})
        log.info("Постер страницы %s загружен в Telegram (%s байт)", page_id, len(data))
    return msg
