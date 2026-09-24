"""Постеры в Telegram (docs/PRODUCT_AND_SCALE.md §6). Скачиваем напрямую с CDN — он не забанен,
туннель не тратим. Первый раз грузим байтами, Telegram отдаёт file_id → pages.poster_file_id;
дальше все сообщения по этому сериалу (уведомления sender'а и карточки бота) идут по file_id:
одна загрузка на сериал, ничего не хранится на диске."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
from urllib.parse import urlsplit

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
POSTER_MAX_PIXELS = 40_000_000       # постеры HDREZKA до ~6 Мп; больше — «бомба», распаковка съест память
Image.MAX_IMAGE_PIXELS = POSTER_MAX_PIXELS


class PosterRejected(Exception):
    """Картинку нельзя отправлять ни в каком виде."""


def safe_url(url: str | None) -> bool:
    """Адрес постера берётся со страницы сайта, а качаем мы его напрямую с сервера, без туннеля. Только
    https и доменное имя: адрес вида http://169.254.169.254/… — это служебный сервис облака, а не CDN."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname or u.hostname == "localhost":
        return False
    try:
        ipaddress.ip_address(u.hostname)
        return False
    except ValueError:
        return True


async def download(url: str) -> bytes | None:
    if not safe_url(url):
        log.warning("Постер %r: адрес не https-домен — не качаю", url)
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POSTER_TIMEOUT)) as http:
            # Без редиректов: иначе проверка адреса выше обходится перенаправлением на внутренний адрес.
            headers = {"User-Agent": cfg.user_agent} if cfg.user_agent else None   # пусто — UA aiohttp
            async with http.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status != 200 or not resp.content_type.startswith("image/"):
                    log.warning("Постер %s: HTTP %s, %s", url, resp.status, resp.content_type)
                    return None
                if (resp.content_length or 0) > POSTER_MAX_BYTES:
                    log.warning("Постер %s: %s байт — больше лимита Telegram", url, resp.content_length)
                    return None
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):   # без длины в заголовке — по частям
                    data += chunk
                    if len(data) > POSTER_MAX_BYTES:
                        log.warning("Постер %s: больше %s байт — не качаю дальше", url, POSTER_MAX_BYTES)
                        return None
                return bytes(data)
    except Exception as exc:  # сеть/таймаут — вызывающий отправит текстом
        log.warning("Постер %s не скачался: %s", url, exc)
        return None


def _to_standard(data: bytes) -> bytes:
    """Постер к стандартному размеру. У HDREZKA отношение сторон гуляет от 0.63 до 0.75, и лента постов
    выглядела лесенкой (12.09.2026). Картинка вписывается целиком — не обрезаем, у постеров текст по краям, —
    а поля закрывает размытая затемнённая копия её же: так это читается как фон, а не как пустые полосы."""
    try:
        src = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:          # больше 2×POSTER_MAX_PIXELS — Pillow сам
        raise PosterRejected(str(exc)) from exc
    with src:
        # Размер известен из заголовка до распаковки: огромную картинку отбрасываем, не раскрывая.
        if src.width * src.height > POSTER_MAX_PIXELS:
            raise PosterRejected(f"{src.width}x{src.height}")
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


async def to_standard(data: bytes) -> bytes | None:
    """Не вышло — отправляем оригинал: лучше постер не того размера, чем пост без картинки.
    None — картинку отправлять нельзя вовсе (PosterRejected): пост уйдёт текстом."""
    try:
        return await asyncio.to_thread(_to_standard, data)
    except (PosterRejected, Image.DecompressionBombError) as exc:
        log.warning("Постер отброшен: %s", exc)
        return None
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
    if data is None:
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
