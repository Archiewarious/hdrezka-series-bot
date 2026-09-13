"""Постеры в Telegram (docs/PRODUCT_AND_SCALE.md §6). Скачиваем напрямую с CDN — он не забанен,
туннель не тратим. Первый раз грузим байтами, Telegram отдаёт file_id → pages.poster_file_id;
дальше все сообщения по этому сериалу (уведомления sender'а и карточки бота) идут по file_id:
одна загрузка на сериал, ничего не хранится на диске."""
from __future__ import annotations

import asyncio
import io
import logging

import aiohttp
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import text

from app.config import cfg

log = logging.getLogger("posters")

CAPTION_MAX_LEN = 1024
POSTER_SIZE = (1000, 1414)           # один размер на все посты: 1:1,414 — самое частое отношение у HDREZKA
POSTER_QUALITY = 88
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


def _to_standard(data: bytes) -> bytes:
    """Постер к стандартному размеру. У HDREZKA отношение сторон гуляет от 0.63 до 0.75, и лента постов
    выглядела лесенкой (12.09.2026). Картинка вписывается целиком — не обрезаем, у постеров текст по краям, —
    а поля закрывает размытая затемнённая копия её же: так это читается как фон, а не как пустые полосы."""
    with Image.open(io.BytesIO(data)) as src:
        im = ImageOps.exif_transpose(src).convert("RGB")
    fitted = ImageOps.contain(im, POSTER_SIZE, Image.LANCZOS)
    if fitted.size == POSTER_SIZE:
        canvas = fitted
    else:
        small = (POSTER_SIZE[0] // 4, POSTER_SIZE[1] // 4)           # размываем уменьшенную копию — быстрее
        canvas = ImageOps.fit(im, small, Image.LANCZOS).filter(ImageFilter.GaussianBlur(8))
        canvas = ImageEnhance.Brightness(canvas).enhance(0.45).resize(POSTER_SIZE, Image.LANCZOS)
        canvas.paste(fitted, ((POSTER_SIZE[0] - fitted.width) // 2, (POSTER_SIZE[1] - fitted.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=POSTER_QUALITY, optimize=True, progressive=True)
    return out.getvalue()


async def to_standard(data: bytes) -> bytes:
    """Не вышло — отправляем оригинал: лучше постер не того размера, чем пост без картинки."""
    try:
        return await asyncio.to_thread(_to_standard, data)
    except Exception as exc:
        log.warning("Постер не привёлся к стандартному размеру (%s) — отправляю как есть", exc)
        return data


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
    data = await to_standard(data)
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
