"""Телеграм-бот: поиск, подписки на страницу или франшизу, выбор озвучки, /my.

Поведение — docs/ARCHITECTURE.md, §6; защита — app/bot/guard.py.
Бот ходит на сайт только по действию пользователя и через тот же клиент
с паузами, что и поллер. В Telegram уходят только ответы пользователю.
Тексты — app/i18n.py (ru / uk / en): язык — users.lang, кэш в процессе (одна реплика).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, Update)
from sqlalchemy import func, select, text

from app import posters
from app import service as svc
from app.bot import guard
from app.bot.search import MAX_WAITING, group_hits
from app.config import cfg
from app.db import init_db, session
from app.i18n import LANGS, fmt_date, t, when
from app.models import Franchise, Page, Schedule, Subscription, User, Voice
from app.rezka.client import AccessBlocked, RezkaClient
from app.rezka.parser import FeedItem, parse_feed
from app.sender import fit_button, watch_url

log = logging.getLogger("bot")
dp = Dispatcher()
client = RezkaClient()
bot: Bot

MAX_RESULTS = 5
SEARCH_TTL = 600
# Из ссылки берём только путь: хост всегда наш. Иначе бот — открытый прокси через туннель.
_PATH_RX = re.compile(r"(/[A-Za-z0-9_\-/]*?/(\d+)-[A-Za-z0-9_\-.]*?\.html)")
SECTION_KEY = {"series": "sec_series", "animation": "sec_animation", "cartoons": "sec_cartoons", "films": "sec_films"}
MENU_KEYS = ("btn_find", "btn_my", "btn_new", "btn_cal", "btn_settings", "btn_help")
# Тексты кнопок меню на всех языках: клавиатура у человека может быть на прежнем языке.
MENU = {k: {t(lang, k) for lang in LANGS} for k in MENU_KEYS}
BOT_USERNAME = "HDRezkaSeriesBot"   # уточняется при старте через get_me()

_cards: dict[int, tuple[FeedItem, float]] = {}
_search_cache: dict[str, tuple[list[FeedItem], float]] = {}
_langs: dict[int, str] = {}
_menus: dict[str, ReplyKeyboardMarkup] = {}

# Только личные чаты. Группы, каналы, другие боты — молча игнорируем.
dp.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.is_bot == False)  # noqa: E712
dp.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

MAX_UPDATE_AGE = timedelta(minutes=15)


class SkipStaleUpdates(BaseMiddleware):
    """Бэклог за время перезапуска обрабатываем (ссылка, присланная во время деплоя, не должна
    пропасть), но не отвечаем на сообщения, которым больше 15 минут: ответ на вчерашний запрос
    только путает. У callback даты нажатия нет — они идут всегда; просроченные Telegram отклоняет сам."""

    async def __call__(self, handler, event: Update, data):
        msg = event.message
        if msg is not None and msg.date is not None:
            sent = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - sent
            if age > MAX_UPDATE_AGE:
                log.info("Пропускаю сообщение от %s: возраст %s", msg.from_user.id if msg.from_user else "?",
                         str(age).split(".")[0])
                return None
        return await handler(event, data)


dp.update.outer_middleware(SkipStaleUpdates())


# ----------------------------------------------------------------------------- язык и меню

async def _lang(user_id: int) -> str:
    """Язык интерфейса из users.lang; кэш на процесс (одна реплика бота, фаза B — Redis)."""
    lang = _langs.get(user_id)
    if lang is None:
        async with session() as s:
            lang = await s.scalar(select(User.lang).where(User.id == user_id))
        if len(_langs) > 20000:
            _langs.clear()
        _langs[user_id] = lang = lang or "ru"
    return lang


async def _touch_user(s, user) -> str:
    """upsert + язык в кэш: новый человек получает язык своего Telegram."""
    lang = await svc.upsert_user(s, user.id, user.username, user.language_code)
    _langs[user.id] = lang
    return lang


def menu(lang: str) -> ReplyKeyboardMarkup:
    kb = _menus.get(lang)
    if kb is None:
        kb = _menus[lang] = ReplyKeyboardMarkup(   # 3×2, docs/PRODUCT_AND_SCALE.md §7.1
            keyboard=[[KeyboardButton(text=t(lang, "btn_find")), KeyboardButton(text=t(lang, "btn_my"))],
                      [KeyboardButton(text=t(lang, "btn_new")), KeyboardButton(text=t(lang, "btn_cal"))],
                      [KeyboardButton(text=t(lang, "btn_settings")), KeyboardButton(text=t(lang, "btn_help"))]],
            resize_keyboard=True, is_persistent=True)
    return kb


def _section_label(lang: str, section: str | None, default: str = "") -> str:
    key = SECTION_KEY.get(section or "")
    return t(lang, key) if key else default


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """(текст, callback_data) или (текст, https-ссылка)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t_, url=d) if d.startswith("https://") else InlineKeyboardButton(text=t_, callback_data=d)
         for t_, d in row] for row in rows])


def _share_url(lang: str, start_arg: str, title: str) -> str:
    """§7.10: кнопка открывает диалог «поделиться» с deep-link на карточку — друг подписывается в одно нажатие."""
    link = f"https://t.me/{BOT_USERNAME}?start={start_arg}"
    return "https://t.me/share/url?" + urlencode({"url": link, "text": t(lang, "share_text", title=title[:60])})


CAL_DAYS = 14


def _remember(items: list[FeedItem]) -> None:
    now = time.time()
    for it in items:
        _cards[it.hdrezka_id] = (it, now)
    if len(_cards) > 5000:
        for k in sorted(_cards, key=lambda k: _cards[k][1])[:1000]:
            _cards.pop(k, None)


async def _site(coro):
    """Запрос к сайту с потолком ожидания: очередь за лимитером не должна вешать бота."""
    return await asyncio.wait_for(coro, timeout=guard.SITE_TIMEOUT)


# ----------------------------------------------------------------------------- команды и меню

@dp.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject) -> None:
    async with session() as s:
        lang = await _touch_user(s, msg.from_user)
        await s.commit()
    arg = (command.args or "").strip()
    # Deep-link «поделиться» (§7.10): p_<hdrezka_id> — карточка страницы, f_<key> — обзор франшизы.
    if arg[:2] in ("p_", "f_") and arg[2:].isdigit():
        async with session() as s:
            if arg[0] == "p":
                page = await svc.page_by_hid(s, int(arg[2:]))
                target = ("page", page.id) if page else None
            else:
                fr = await s.scalar(select(Franchise).where(Franchise.key_hdrezka_id == int(arg[2:])))
                target = ("franchise", fr.id) if fr else None
        if target is None:
            await msg.answer(t(lang, "start_unknown"), reply_markup=menu(lang))
        elif target[0] == "page":
            await msg.answer(t(lang, "card_by_link"), reply_markup=menu(lang))
            await _send_card(msg, msg.from_user.id, target[1])
        else:
            text_, kb = await _render_franchise_overview(msg.from_user.id, target[1], None)
            await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)
        return
    await msg.answer(t(lang, "start"), reply_markup=menu(lang))


@dp.message(Command("help"))
@dp.message(F.text.in_(MENU["btn_help"]))
async def cmd_help(msg: Message) -> None:
    lang = await _lang(msg.from_user.id)
    await msg.answer(t(lang, "help"), reply_markup=menu(lang))


@dp.message(Command("my"))
@dp.message(F.text.in_(MENU["btn_my"]))
async def cmd_my(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    text_, kb = await _render_my(msg.from_user.id)
    await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)


@dp.message(F.text.in_(MENU["btn_find"]))
async def btn_find(msg: Message) -> None:
    lang = await _lang(msg.from_user.id)
    await msg.answer(t(lang, "find_prompt"), reply_markup=menu(lang))


@dp.message(Command("new"))
@dp.message(F.text.in_(MENU["btn_new"]))
async def cmd_new(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    text_, kb = await _render_new(msg.from_user.id)
    await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)


@dp.message(Command("settings"))
@dp.message(F.text.in_(MENU["btn_settings"]))
async def cmd_settings(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    async with session() as s:
        await _touch_user(s, msg.from_user)
        await s.commit()
    text_, kb = await _render_settings(msg.from_user.id)
    await msg.answer(text_, reply_markup=kb)


@dp.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if msg.from_user.id not in cfg.admin_ids:
        return
    async with session() as s:
        users = await s.scalar(select(func.count()).select_from(User))
        active = await s.scalar(select(func.count()).select_from(User).where(User.is_active))
        sp = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.scope == "page"))
        sf = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.scope == "franchise"))
        pages = await s.scalar(select(func.count()).select_from(Page))
        unread = await s.scalar(select(func.count()).select_from(Page).where(Page.page_refreshed_at.is_(None)))
        frs = await s.scalar(select(func.count()).select_from(Franchise))
        pending = await s.scalar(text("SELECT count(*) FROM notifications WHERE status = 'pending'"))
        sent = await s.scalar(text("SELECT count(*) FROM notifications WHERE status = 'sent'"))
        langs = (await s.execute(text("SELECT lang, count(*) FROM users GROUP BY lang ORDER BY 2 DESC"))).all()
        last = await svc.meta_get(s, "last_poll_ok")
        stale = await svc.meta_get(s, "poller_stale") == "1"
    await msg.answer(
        ("⚠️ Поллер молчит дольше порога — проверьте туннель и логи\n" if stale else "")
        + f"Пользователей: {users} (активных {active}; " + ", ".join(f"{l} {n}" for l, n in langs) + ")\n"
        f"Подписок: на страницы {sp}, на франшизы {sf}\n"
        f"Страниц в базе: {pages} (не прочитано {unread}), франшиз: {frs}\n"
        f"Уведомлений: в очереди {pending}, отправлено (7 дн.) {sent}\n"
        f"Последний обход: {last}")


# ----------------------------------------------------------------------------- текст: ссылка или поиск

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    query = guard.clean_query(msg.text)
    lang = await _lang(msg.from_user.id)
    if len(query) < 2:
        await msg.answer(t(lang, "query_short"), reply_markup=menu(lang))
        return
    link = _PATH_RX.search(msg.text)
    limiter = guard.site_actions if link else guard.cheap_actions   # локальный поиск сайт не трогает
    if not limiter.allow(msg.from_user.id):
        await msg.answer(t(lang, "too_fast"))
        return
    async with session() as s:
        lang = await _touch_user(s, msg.from_user)
        await s.commit()
    if link:
        await _handle_link(msg, lang, int(link.group(2)), link.group(1))
    else:
        await _handle_search(msg, lang, query)


@dp.message()
async def on_other(msg: Message) -> None:
    """Стикеры, фото, голосовые — подсказываем, что бот понимает только текст."""
    if guard.cheap_actions.allow(msg.from_user.id):
        lang = await _lang(msg.from_user.id)
        await msg.answer(t(lang, "text_only"), reply_markup=menu(lang))


async def _search(query: str) -> list[FeedItem]:
    key = query.lower()
    hit = _search_cache.get(key)
    if hit and time.time() - hit[1] < SEARCH_TTL:
        return hit[0]
    items = parse_feed(await _site(client.search(query)))
    if len(_search_cache) > 2000:
        _search_cache.clear()
    _search_cache[key] = (items, time.time())
    return items


async def _handle_search(msg: Message, lang: str, query: str) -> None:
    """Сначала каталог (миллисекунды, без сайта); сайт — когда каталог не знает или по кнопке."""
    async with session() as s:
        g = group_hits(await svc.search_catalog(s, query))
        stats = await svc.franchise_stats(s, g.franchise_ids) if g.franchise_ids else {}
    if g.empty:
        if not guard.site_actions.allow(msg.from_user.id):
            await msg.answer(t(lang, "too_fast"))
            return
        note = await msg.answer(t(lang, "searching"))
        await _site_search(note, lang, query, local_hidden=g.hidden)
        return
    rows = []
    for fid in g.franchise_ids:
        name, parts, ongoing = stats.get(fid, (t(lang, "franchise_word"), 0, 0))
        tail = t(lang, "parts_n", n=parts) + ", " + (t(lang, "ongoing_n", n=ongoing) if ongoing else t(lang, "nothing_airing"))
        rows.append([(f"🎞 {name[:22]} · {tail}", f"subf:{fid}:{g.origin[fid]}")])
    rows += [[(f"➕ {p.title[:34]} · {_section_label(lang, p.section)} · {p.last_season}×{p.last_episode}",
               f"sub:{p.hdrezka_id}")] for p in g.standalone]
    # Завершённые без франшизы — карточкой: там кнопка «🔔 Сообщить о продолжении».
    rows += [[(f"🔔 {p.title[:34]} · {t(lang, 'finished_word')}", f"pcard:{p.id}")] for p in g.waiting]
    rows.append([(t(lang, "btn_search_site"), f"site:{_site_token(query)}")])
    text_ = t(lang, "found_n", n=len(rows) - 1)
    if g.hidden:
        text_ += t(lang, "hidden_local", n=g.hidden)
    await msg.answer(text_, reply_markup=_kb(rows), disable_web_page_preview=True)


_site_queries: dict[str, tuple[str, float]] = {}


def _site_token(query: str) -> str:
    """callback_data ≤ 64 байт — запрос держим в памяти по короткому ключу (одна реплика бота)."""
    tok = hashlib.blake2b(query.lower().encode(), digest_size=6).hexdigest()
    if len(_site_queries) > 2000:
        _site_queries.clear()
    _site_queries[tok] = (query, time.time())
    return tok


@dp.callback_query(F.data.startswith("site:"))
async def cb_site_search(cb: CallbackQuery) -> None:
    lang = await _lang(cb.from_user.id)
    hit = _site_queries.get(cb.data.split(":", 1)[1])
    if not hit or time.time() - hit[1] > SEARCH_TTL * 6:
        await cb.answer(t(lang, "query_stale"), show_alert=True)
        return
    if not guard.site_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"))
        return
    await cb.answer()
    note = await cb.message.answer(t(lang, "searching_site"))
    await _site_search(note, lang, hit[0])


async def _site_search(note: Message, lang: str, query: str, local_hidden: int = 0) -> None:
    try:
        items = await _search(query)
    except (AccessBlocked, asyncio.TimeoutError):
        await note.edit_text(t(lang, "busy"))
        return
    _remember(items)
    # Каталог-first: все карточки ответа — в pages (страницы дочитает очередь поллера).
    # База учится на пользователях: следующий такой поиск ответит без сайта.
    finished_known: set[int] = set()
    franchises: list = []
    page_ids: dict[int, int] = {}          # hdrezka_id → pages.id для страниц без франшизы
    async with session() as s:
        known = [i.hdrezka_id for i in items]
        for i in items:
            pg = await svc.upsert_page_from_feed(s, i)
            if pg.franchise_id is None:
                page_ids[i.hdrezka_id] = pg.id
        if known:
            # База знает больше карточки: сезон, завершённый по расписанию (сайт его так не пометил),
            # в «Сейчас выходит» не попадает — иначе предложим подписку на то, где серий не будет.
            finished_known = set((await s.execute(select(Page.hdrezka_id).where(
                Page.hdrezka_id.in_(known), Page.is_finished.is_(True)))).scalars())
            franchises = (await s.execute(
                select(Franchise.id, Franchise.name, func.min(Page.id))
                .join(Page, Page.franchise_id == Franchise.id)
                .where(Page.hdrezka_id.in_(known))
                .group_by(Franchise.id, Franchise.name))).all()
        await s.commit()
    ongoing = [i for i in items if i.section in cfg.feed_sections and i.has_episode
               and i.hdrezka_id not in finished_known][:MAX_RESULTS]
    # Франшиза известна — отдельной строкой: «следить за всем новым» одним нажатием.
    fr_rows = [[(t(lang, "franchise_row", name=name[:28]), f"subf:{fid}:{pid}")] for fid, name, pid in franchises]
    # Завершённые без франшизы — карточкой: там «🔔 Сообщить о продолжении».
    waiting = [i for i in items if (i.is_finished or i.hdrezka_id in finished_known) and not i.looks_like_film
               and i.section in cfg.feed_sections and i.hdrezka_id in page_ids][:MAX_WAITING]
    fr_rows += [[(f"🔔 {i.title[:34]} · {t(lang, 'finished_word')}", f"pcard:{page_ids[i.hdrezka_id]}")] for i in waiting]
    if not ongoing:
        hidden = local_hidden + sum(1 for i in items if i.is_finished or i.looks_like_film
                                    or i.hdrezka_id in finished_known) - len(waiting)
        text_ = t(lang, "nothing_airing_query") + (t(lang, "hidden_site", n=hidden) if hidden > 0 else "")
        if franchises:
            text_ += t(lang, "has_franchise_hint")
        if waiting:
            text_ += t(lang, "waiting_hint")
        if not franchises and not waiting:
            text_ += t(lang, "link_hint")
        await note.edit_text(text_, reply_markup=_kb(fr_rows) if fr_rows else None)
        return
    # Каждая кнопка понятна без контекста: название · раздел · текущая серия.
    rows = [[(f"➕ {i.title[:34]} · {_section_label(lang, i.section, i.section or '')} · {i.season}×{i.episode}",
              f"sub:{i.hdrezka_id}")] for i in ongoing]
    await note.edit_text(t(lang, "airing_now_n", n=len(ongoing)), reply_markup=_kb(rows + fr_rows))


PAGE_FRESH_DAYS = 7   # страница в базе моложе — на сайт не ходим: бот учится на пользователях


async def _handle_link(msg: Message, lang: str, hdrezka_id: int, path: str) -> None:
    """Ссылка → карточка с кнопкой. Подписка — только по нажатию.
    Если страницу уже читали недавно (кто-то подписывался) — отвечаем из базы."""
    async with session() as s:
        page = await svc.page_by_hid(s, hdrezka_id)
        known = page is not None and page.page_refreshed_at is not None \
            and (svc.now() - page.page_refreshed_at).days < PAGE_FRESH_DAYS
        if known:
            page_id, title, finished, last_event = page.id, page.title, page.is_finished, page.last_event_at

    if known:
        log.info("Ссылка на %s — из базы, без запроса к сайту", hdrezka_id)
        note = await _send_card(msg, msg.from_user.id, page_id)
        # Флаг «завершён» знаем наверняка, если он уже стоит или серии выходили недавно.
        recently_active = last_event is not None and (svc.now() - last_event).days < 30
        if not finished and not recently_active:
            asyncio.create_task(_refine_finished(note, msg.from_user.id, hdrezka_id, page_id, title))
        return

    url = f"{client.base_url}{path}"
    note = await msg.answer(t(lang, "reading_page"))
    try:
        async with session() as s:
            res = await _site(svc.sync_page(s, client, hdrezka_id, url))
            await s.commit()
            page_id, title = res.page.id, res.page.title
    except (AccessBlocked, asyncio.TimeoutError):
        await note.edit_text(t(lang, "busy"))
        return
    try:
        await note.delete()
    except TelegramBadRequest:
        pass
    note = await _send_card(msg, msg.from_user.id, page_id)
    # Флаг «завершён» отдаёт только лента — проверяем в фоне, чтобы не держать человека в ожидании.
    asyncio.create_task(_refine_finished(note, msg.from_user.id, hdrezka_id, page_id, title))


async def _refine_finished(note: Message, user_id: int, hdrezka_id: int, page_id: int, title: str) -> None:
    try:
        items = await _search(title)
    except (AccessBlocked, asyncio.TimeoutError):
        return
    card = next((i for i in items if i.hdrezka_id == hdrezka_id), None)
    if not card:
        return
    async with session() as s:
        await svc.upsert_page_from_feed(s, card)
        await s.commit()
    if card.is_finished:
        text_, kb = await _render_page_card(user_id, page_id)
        await _edit_message(note, text_, kb)


# ----------------------------------------------------------------------------- карточка страницы

async def _render_page_card(user_id: int, page_id: int, just_created: bool = False):
    lang = await _lang(user_id)
    async with session() as s:
        page = await s.get(Page, page_id)
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user_id,
                                                        Subscription.page_id == page_id))
        nxt = await s.scalar(select(func.min(Schedule.air_date)).where(
            Schedule.page_id == page_id, Schedule.aired.is_(False), Schedule.air_date >= date.today()))
        voices = (await s.execute(select(Voice).where(Voice.page_id == page_id))).scalars().all()
        fr = await s.get(Franchise, page.franchise_id) if page.franchise_id else None
        parts = (await s.scalar(select(func.count()).select_from(Page).where(Page.franchise_id == page.franchise_id))
                 if fr else 0)
        fr_sub = fr and await s.scalar(select(Subscription.id).where(
            Subscription.user_id == user_id, Subscription.franchise_id == fr.id))

    kind = _section_label(lang, page.section)
    if page.content_type == "film":
        kind = t(lang, "kind_film")
    state = svc.card_state(page, sub is not None, bool(fr_sub), fr is not None)
    if state == "waiting":
        head = t(lang, "head_wait_created" if just_created else "head_waiting")
    elif state == "subscribed":
        head = t(lang, "head_subscribed_new" if just_created else "head_in_subs")
    else:
        head = t(lang, "head_in_subs" if state == "franchise_sub" else "head_found")
    lines = [f"{head} <b>{page.title}</b>" + (f" · {kind}" if kind else "")]
    if page.content_type != "film" and page.last_season:
        if page.is_finished:
            lines.append(t(lang, "last_episode_finished", s=page.last_season, e=page.last_episode))
        else:
            lines.append(t(lang, "now_airing", s=page.last_season, e=page.last_episode))
    if nxt:
        lines.append(t(lang, "next_episode", d=fmt_date(lang, nxt)))
    rows = []
    if state == "waiting":
        lines.append(t(lang, "wait_desc"))
        rows.append([(t(lang, "btn_stop_waiting"), f"unsub:{sub.id}")])
    elif state == "subscribed":
        lines.append(t(lang, "voice_line", v=_voice_label(lang, sub, voices)))
        rows.append([(t(lang, "btn_choose_voice"), f"voices:{sub.id}")])
        rows.append([(t(lang, "btn_unsubscribe"), f"unsub:{sub.id}")])
    elif state == "franchise_sub":
        # Раньше здесь не было ни одной кнопки: человек не понимал, подписан он или нет (07.09.2026).
        lines.append(t(lang, "in_franchise_sub", name=fr.name))
        rows.append([(t(lang, "btn_open_franchise", name=fr.name[:24]), f"fcard:{fr.id}")])
        rows.append([(t(lang, "btn_unfollow_fr"), f"unsubq:{fr_sub}")])
    elif state == "film":
        lines.append(t(lang, "film_no_sub"))
    elif state == "finished_alone":
        lines.append(t(lang, "finished_offer_wait"))
        rows.append([(t(lang, "btn_wait"), f"wait:{page.hdrezka_id}")])
    elif state == "finished_franchise":
        lines.append(t(lang, "finished_in_franchise"))
    else:
        # «Нажал и забыл»: франшиза известна — подписываем на неё целиком, иначе на страницу
        # (она сама станет «жду продолжения», когда сезон закончится).
        lines.append(t(lang, "follow_desc_franchise" if fr else "follow_desc"))
        rows.append([(t(lang, "btn_follow"), f"subf_all:{fr.id}" if fr else f"sub:{page.hdrezka_id}")])
    if fr and parts > 1 and state != "franchise_sub":
        # Кнопка «Следить» уже покрывает всю франшизу — здесь только выбор отдельных частей.
        label = (t(lang, "btn_fr_parts_n", n=parts) if state == "follow"
                 else t(lang, "btn_whole_franchise", name=fr.name[:24], n=parts))
        rows.append([(label, f"subf:{fr.id}:{page.id}")])
    if page.content_type != "film":
        rows.append([(t(lang, "btn_schedule"), f"sched:p:{page.id}"), (t(lang, "btn_open_site"), page.url)])
    else:
        rows.append([(t(lang, "btn_open_site"), page.url)])
    rows.append([(t(lang, "btn_share"), _share_url(lang, f"p_{page.hdrezka_id}", page.title))])
    return "\n".join(lines) + t(lang, "hint"), _kb(rows)


async def _send_card(target: Message, user_id: int, page_id: int, just_created: bool = False) -> Message:
    """Карточка новым сообщением: с постером, если он известен (§7.4), иначе текстом."""
    text_, kb = await _render_page_card(user_id, page_id, just_created)
    sent = None
    async with session() as s:
        row = (await s.execute(text("SELECT poster_url, poster_file_id FROM pages WHERE id = :p"), {"p": page_id})).first()
        if row and row[0]:
            sent = await posters.send_photo_cached(target.bot, s, target.chat.id, page_id, row[0], row[1], text_, kb)
        await s.commit()
    return sent or await target.answer(text_, reply_markup=kb, disable_web_page_preview=True)


def _voice_label(lang: str, sub: Subscription | None, voices: list[Voice]) -> str:
    if not sub or not sub.voice_filter:
        return t(lang, "voice_any_n", n=len(voices)) if voices else t(lang, "voice_any")
    names = {v.translator_id: v.name for v in voices}
    return ", ".join(names.get(t_, f"#{t_}") for t_ in sub.voice_filter)


async def _too_many_subs(user_id: int) -> bool:
    async with session() as s:
        n = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id))
    return n >= guard.MAX_SUBSCRIPTIONS


@dp.callback_query(F.data.regexp(r"^(sub|wait):"))
async def cb_subscribe(cb: CallbackQuery) -> None:
    """sub: — подписка на выходящий сезон; wait: — на завершённый, со смыслом «жду продолжения»."""
    lang = await _lang(cb.from_user.id)
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"), show_alert=False)
        return
    hdrezka_id = int(cb.data.split(":")[1])
    if await _too_many_subs(cb.from_user.id):
        await cb.answer(t(lang, "max_subs", n=guard.MAX_SUBSCRIPTIONS), show_alert=True)
        return
    async with session() as s:
        lang = await _touch_user(s, cb.from_user)
        page = await svc.page_by_hid(s, hdrezka_id)
        if page is None:
            card = _cards.get(hdrezka_id)
            if not card:
                await s.commit()
                await cb.answer(t(lang, "card_stale"), show_alert=True)
                return
            page = await svc.upsert_page_from_feed(s, card[0])
        waiting = cb.data.startswith("wait:")
        if page.content_type == "film" or (page.is_finished and not waiting):
            await s.commit()
            await cb.answer(t(lang, "cannot_sub"), show_alert=True)
            return
        created = await svc.subscribe_page(s, cb.from_user.id, page.id)
        await s.commit()
        pid, hid, url, fresh = page.id, page.hdrezka_id, page.url, page.page_refreshed_at
    await cb.answer()

    # В выдаче поиска кнопка становится «✓ …» — результат виден без тоста.
    if cb.message and cb.message.reply_markup and cb.message.reply_markup.inline_keyboard:
        tapped = any(b.callback_data == cb.data and b.text.startswith(("➕", "🔔"))
                     for row in cb.message.reply_markup.inline_keyboard for b in row)
        if tapped:
            rows = [[InlineKeyboardButton(
                text=("✓ " + b.text[2:]) if b.callback_data == cb.data else b.text,
                callback_data="noop" if b.callback_data == cb.data else b.callback_data) for b in row]
                for row in cb.message.reply_markup.inline_keyboard]
            try:
                await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            except TelegramBadRequest:
                pass
            send_new = True
        else:
            send_new = False   # нажали в карточке — обновим её же
    else:
        send_new = True

    if not fresh or (svc.now() - fresh).days >= 1:
        try:
            async with session() as s:
                await _site(svc.sync_page(s, client, hid, url))
                await s.commit()
        except (AccessBlocked, asyncio.TimeoutError):
            log.warning("Не прочитал страницу %s после подписки", hid)

    if send_new:
        await _send_card(cb.message, cb.from_user.id, pid, just_created=created)
    else:
        text_, kb = await _render_page_card(cb.from_user.id, pid, just_created=created)
        await _edit(cb, text_, kb)


# ----------------------------------------------------------------------------- франшиза

def _part_status(lang: str, p: Page) -> tuple[str, bool]:
    """Что известно о части: (подпись, можно ли подписаться на неё отдельно)."""
    if p.content_type == "film":
        return t(lang, "kind_film"), False
    kind = _section_label(lang, p.section, t(lang, "kind_series") if p.content_type else "")
    if p.is_finished:
        return t(lang, "st_finished", kind=kind) + (f", {p.last_season}×{p.last_episode}" if p.last_episode else ""), False
    if p.last_episode:
        return t(lang, "st_airing", kind=kind, s=p.last_season, e=p.last_episode), True
    return (kind + " · " if kind else "") + t(lang, "st_unread"), False


async def _render_franchise_overview(user_id: int, fid: int, origin_page_id: int | None):
    lang = await _lang(user_id)
    async with session() as s:
        fr = await s.get(Franchise, fid)
        if fr is None:
            return t(lang, "fr_not_found"), _kb([])
        sub = await s.scalar(select(Subscription.id).where(Subscription.user_id == user_id,
                                                           Subscription.franchise_id == fid))
        parts = (await s.execute(select(Page).where(Page.franchise_id == fid)
                                 .order_by(Page.year.desc().nulls_last(), Page.id.desc()))).scalars().all()
        my_pages = set((await s.execute(select(Subscription.page_id).where(
            Subscription.user_id == user_id, Subscription.page_id.isnot(None)))).scalars())
    ongoing = [p for p in parts if _part_status(lang, p)[1]]
    rest = [p for p in parts if not _part_status(lang, p)[1]]
    lines = [t(lang, "fr_head", name=fr.name, parts=t(lang, "parts_n", n=len(parts)))]
    if sub:
        lines.append(t(lang, "fr_subscribed"))
    if ongoing:
        lines.append(t(lang, "fr_airing"))
        lines += [f"  • {p.title[:40]} · {_part_status(lang, p)[0]}" + (" ✓" if p.id in my_pages else "") for p in ongoing]
    if rest:
        lines.append(t(lang, "fr_rest"))
        lines += [f"  • {p.title[:40]} · {p.year or ''} · {_part_status(lang, p)[0]}" for p in rest[:12]]
        if len(rest) > 12:
            lines.append(t(lang, "fr_more", n=len(rest) - 12))
    lines.append(t(lang, "fr_explain"))
    rows = []
    if not sub:
        rows.append([(t(lang, "btn_sub_franchise_all"), f"subf_all:{fid}")])
    for p in ongoing[:6]:
        if p.id not in my_pages and not sub:
            rows.append([(f"➕ {p.title[:36]} · {p.last_season}×{p.last_episode}", f"sub:{p.hdrezka_id}")])
    if sub:
        rows.append([(t(lang, "btn_fr_card"), f"fcard:{fid}")])
    rows.append([(t(lang, "btn_share"), _share_url(lang, f"f_{fr.key_hdrezka_id}", fr.name))])
    if origin_page_id:
        rows.append([(t(lang, "btn_back"), f"pcard:{origin_page_id}")])
    return "\n".join(lines), _kb(rows)


PREFETCH_PARTS = 3


async def _prefetch_parts(m: Message, user_id: int, fid: int, origin: int | None) -> None:
    """Обзор франшизы открыли, а верхние части ещё не читали («ещё не смотрели»): дочитываем до трёх
    в фоне и обновляем сообщение. Остальное — очередь поллера. Считается действием на сайте (лимит)."""
    async with session() as s:
        parts = (await s.execute(
            select(Page.hdrezka_id, Page.url).where(Page.franchise_id == fid, Page.page_refreshed_at.is_(None), Page.url != "")
            .order_by(Page.year.desc().nulls_last(), Page.id.desc()).limit(PREFETCH_PARTS))).all()
    if not parts or not guard.site_actions.allow(user_id):
        return
    done = 0
    for hid, url in parts:
        try:
            async with session() as s:
                await _site(svc.sync_page(s, client, hid, url))
                await s.commit()
            done += 1
        except (AccessBlocked, asyncio.TimeoutError):
            break
    if done:
        text_, kb = await _render_franchise_overview(user_id, fid, origin)
        await _edit_message(m, text_, kb)


@dp.callback_query(F.data.startswith("subf:"))
async def cb_franchise_overview(cb: CallbackQuery) -> None:
    """«Вся франшиза» сначала показывает состав, подписка — отдельным нажатием."""
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    parts = cb.data.split(":")
    fid = int(parts[1])
    origin = int(parts[2]) if len(parts) > 2 else None
    text_, kb = await _render_franchise_overview(cb.from_user.id, fid, origin)
    await cb.answer()
    await _edit(cb, text_, kb)
    asyncio.create_task(_prefetch_parts(cb.message, cb.from_user.id, fid, origin))


@dp.callback_query(F.data.startswith("subf_all:"))
async def cb_subscribe_franchise(cb: CallbackQuery) -> None:
    lang = await _lang(cb.from_user.id)
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"))
        return
    fid = int(cb.data.split(":")[1])
    if await _too_many_subs(cb.from_user.id):
        await cb.answer(t(lang, "max_subs", n=guard.MAX_SUBSCRIPTIONS), show_alert=True)
        return
    async with session() as s:
        lang = await _touch_user(s, cb.from_user)
        if await s.get(Franchise, fid) is None:
            await cb.answer(t(lang, "fr_not_found"), show_alert=True)
            return
        created = await svc.subscribe_franchise(s, cb.from_user.id, fid)
        await s.commit()
    await cb.answer()
    text_, kb = await _render_franchise_card(cb.from_user.id, fid, created)
    await _edit(cb, text_, kb)


async def _render_franchise_card(user_id: int, fid: int, created: bool = True):
    lang = await _lang(user_id)
    async with session() as s:
        fr = await s.get(Franchise, fid)
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user_id,
                                                        Subscription.franchise_id == fid))
        parts = (await s.execute(select(Page).where(Page.franchise_id == fid)
                                 .order_by(Page.year.desc().nulls_last(), Page.id.desc()))).scalars().all()
        voices = (await s.execute(select(Voice).join(Page, Page.id == Voice.page_id)
                                  .where(Page.franchise_id == fid))).scalars().all()
    ongoing = [p for p in parts if not p.is_finished and p.last_episode and p.content_type != "film"]
    head = t(lang, "fc_head_new" if (sub and created) else ("fc_head_in_subs" if sub else "fc_head"))
    lines = [f"{head} <b>{fr.name}</b> ({t(lang, 'parts_n', n=len(parts))})"]
    if ongoing:
        lines.append(t(lang, "fc_airing"))
        lines += [f"  • {p.title[:44]} — {p.last_season}×{p.last_episode}" for p in ongoing[:5]]
    lines.append(t(lang, "fc_desc"))
    uniq = list({v.translator_id: v for v in voices}.values())
    rows = []
    if sub:
        lines.append(t(lang, "voice_line", v=_voice_label(lang, sub, uniq)))
        rows.append([(t(lang, "btn_choose_voice"), f"voices:{sub.id}")])
        rows.append([(t(lang, "btn_unsubscribe"), f"unsub:{sub.id}")])
    else:
        rows.append([(t(lang, "btn_sub_franchise"), f"subf_all:{fid}")])
    rows.append([(t(lang, "btn_schedule"), f"sched:f:{fid}")])
    rows.append([(t(lang, "btn_fr_parts"), f"subf:{fid}")])
    rows.append([(t(lang, "btn_share"), _share_url(lang, f"f_{fr.key_hdrezka_id}", fr.name))])
    return "\n".join(lines) + t(lang, "hint"), _kb(rows)


# ----------------------------------------------------------------------------- расписание и календарь

# Неделя назад (✓ — уже вышли) и всё, что запланировано вперёд.
SCHED_TEMPLATE = """
    SELECT p.id, p.title, sc.season, sc.episode, sc.air_date, sc.aired
      FROM schedule sc JOIN pages p ON p.id = sc.page_id
     WHERE {where} AND coalesce(p.content_type, 'series') = 'series'
       AND sc.air_date IS NOT NULL AND sc.air_date >= current_date - 7
     ORDER BY sc.air_date, p.title, sc.season, sc.episode
     LIMIT 40
"""


def _fmt_sched(lang: str, rows, with_title: bool) -> list[str]:
    out = []
    for _pid, title, season, episode, d, aired in rows:
        mark = "✓" if aired or d < date.today() else "•"
        who = f"<b>{title[:30]}</b> " if with_title else ""
        out.append(f"{mark} {who}{season}×{episode} — {when(lang, d)}")
    return out


async def _render_schedule(user_id: int, kind: str, obj_id: int):
    lang = await _lang(user_id)
    async with session() as s:
        if kind == "p":
            page = await s.get(Page, obj_id)
            head = f"📅 <b>{page.title}</b>" if page else "📅"
            rows = (await s.execute(text(SCHED_TEMPLATE.format(where="p.id = :id")), {"id": obj_id})).all()
            back = (t(lang, "btn_to_series"), f"pcard:{obj_id}")
        else:
            fr = await s.get(Franchise, obj_id)
            head = t(lang, "sched_fr_head", name=fr.name) if fr else "📅"
            rows = (await s.execute(text(SCHED_TEMPLATE.format(where="p.franchise_id = :id")), {"id": obj_id})).all()
            back = (t(lang, "btn_to_franchise"), f"fcard:{obj_id}")
    lines = _fmt_sched(lang, rows, with_title=(kind == "f"))
    body = "\n".join(lines) if lines else t(lang, "sched_empty")
    return f"{head}\n\n{body}{t(lang, 'cal_note')}", _kb([[back]])


@dp.callback_query(F.data.startswith("sched:"))
async def cb_schedule(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    _, kind, obj_id = cb.data.split(":")
    text_, kb = await _render_schedule(cb.from_user.id, kind, int(obj_id))
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("pcard:"))
async def cb_page_card(cb: CallbackQuery) -> None:
    text_, kb = await _render_page_card(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("fcard:"))
async def cb_franchise_card(cb: CallbackQuery) -> None:
    text_, kb = await _render_franchise_card(cb.from_user.id, int(cb.data.split(":")[1]), created=False)
    await cb.answer()
    await _edit(cb, text_, kb)


CALENDAR_SQL = text("""
    SELECT DISTINCT p.title, sc.season, sc.episode, sc.air_date
      FROM subscriptions sub
      JOIN pages p ON (p.id = sub.page_id OR (sub.franchise_id IS NOT NULL AND p.franchise_id = sub.franchise_id))
      JOIN schedule sc ON sc.page_id = p.id
     WHERE sub.user_id = :uid AND NOT sc.aired AND sc.air_date IS NOT NULL
       AND sc.air_date >= current_date AND sc.air_date < current_date + :days
       AND coalesce(p.content_type, 'series') = 'series'
     ORDER BY sc.air_date, p.title, sc.season, sc.episode
     LIMIT 60
""")


@dp.message(Command("calendar"))
@dp.message(F.text.in_(MENU["btn_cal"]))
async def cmd_calendar(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    lang = await _lang(msg.from_user.id)
    async with session() as s:
        rows = (await s.execute(CALENDAR_SQL, {"uid": msg.from_user.id, "days": CAL_DAYS})).all()
        has_subs = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == msg.from_user.id))
    if not has_subs:
        await msg.answer(t(lang, "cal_no_subs"), reply_markup=menu(lang))
        return
    if not rows:
        await msg.answer(t(lang, "cal_empty", n=CAL_DAYS) + t(lang, "cal_note"), reply_markup=menu(lang))
        return
    lines, cur = [t(lang, "cal_head", n=CAL_DAYS)], None
    for title, season, episode, d in rows:
        if d != cur:
            cur = d
            lines.append(f"\n<b>{when(lang, d)}</b>")
        lines.append(f"  • {title[:36]} — {season}×{episode}")
    await msg.answer("\n".join(lines) + t(lang, "cal_note"), reply_markup=menu(lang))


# ----------------------------------------------------------------------------- озвучки

async def _voices_for_sub(s, sub: Subscription) -> list[Voice]:
    if sub.page_id:
        q = select(Voice).where(Voice.page_id == sub.page_id)
    else:
        q = select(Voice).join(Page, Page.id == Voice.page_id).where(Page.franchise_id == sub.franchise_id)
    uniq: dict[int, Voice] = {}
    for v in (await s.execute(q)).scalars():
        uniq.setdefault(v.translator_id, v)
    return sorted(uniq.values(), key=lambda v: v.name.lower())


async def _own_sub(user_id: int, sub_id: int) -> Subscription | None:
    async with session() as s:
        sub = await s.get(Subscription, sub_id)
    return sub if sub and sub.user_id == user_id else None


async def _render_voices(user_id: int, sub_id: int):
    lang = await _lang(user_id)
    sub = await _own_sub(user_id, sub_id)
    if not sub:
        return t(lang, "sub_not_found"), _kb([])
    async with session() as s:
        voices = await _voices_for_sub(s, sub)
    chosen = set(sub.voice_filter or [])
    rows = [[(("☑ " if not chosen else "☐ ") + t(lang, "voice_any_btn"), f"vany:{sub_id}")]]
    rows += [[(f"{'☑' if v.translator_id in chosen else '☐'} {v.name[:36]}", f"vt:{sub_id}:{v.translator_id}")]
             for v in voices[:40]]
    rows.append([(t(lang, "btn_done"), f"card:{sub_id}")])
    hint = t(lang, "voices_hint") if voices else t(lang, "voices_unknown")
    return hint, _kb(rows)


@dp.callback_query(F.data.startswith("voices:"))
async def cb_voices(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    text_, kb = await _render_voices(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("vt:"))
async def cb_voice_toggle(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    _, sub_id, tid = cb.data.split(":")
    if sub := await _own_sub(cb.from_user.id, int(sub_id)):
        async with session() as s:
            cur = set(sub.voice_filter or []) ^ {int(tid)}
            await svc.set_voice_filter(s, cb.from_user.id, sub.id, sorted(cur) or None)
            await s.commit()
    text_, kb = await _render_voices(cb.from_user.id, int(sub_id))
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("vany:"))
async def cb_voice_any(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with session() as s:
        await svc.set_voice_filter(s, cb.from_user.id, sub_id, None)
        await s.commit()
    text_, kb = await _render_voices(cb.from_user.id, sub_id)
    await cb.answer(t(await _lang(cb.from_user.id), "voice_any_btn"))
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("card:"))
async def cb_card(cb: CallbackQuery) -> None:
    sub = await _own_sub(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer()
    if not sub:
        await _edit(cb, t(await _lang(cb.from_user.id), "sub_not_found"), _kb([]))
        return
    if sub.page_id:
        text_, kb = await _render_page_card(cb.from_user.id, sub.page_id)
    else:
        text_, kb = await _render_franchise_card(cb.from_user.id, sub.franchise_id, created=False)
    await _edit(cb, text_, kb)


# ----------------------------------------------------------------------------- /my и отписка

async def _render_my(user_id: int):
    lang = await _lang(user_id)
    async with session() as s:
        subs = (await s.execute(select(Subscription).where(Subscription.user_id == user_id)
                                .order_by(Subscription.scope, Subscription.created_at))).scalars().all()
        if not subs:
            return t(lang, "my_empty"), _kb([])
        lines, rows = [t(lang, "my_head", n=len(subs))], []
        for sub in subs:
            if sub.scope == "franchise":
                fr = await s.get(Franchise, sub.franchise_id)
                n = await s.scalar(select(func.count()).select_from(Page).where(Page.franchise_id == fr.id))
                ongoing = (await s.execute(select(Page).where(Page.franchise_id == fr.id, Page.is_finished.is_(False),
                                                              Page.last_episode.isnot(None), Page.content_type != "film"))).scalars().all()
                tail = "; ".join(f"{p.title[:28]} {p.last_season}×{p.last_episode}" for p in ongoing[:2])
                lines.append(t(lang, "my_fr_line", name=fr.name, parts=t(lang, "parts_n", n=n))
                             + (t(lang, "my_airing", tail=tail) if tail else ""))
                label = fr.name
            else:
                p = await s.get(Page, sub.page_id)
                nxt = await s.scalar(select(func.min(Schedule.air_date)).where(
                    Schedule.page_id == p.id, Schedule.aired.is_(False), Schedule.air_date >= date.today()))
                st = f"{p.last_season}×{p.last_episode}" if p.last_episode else "—"
                extra = t(lang, "my_next", d=fmt_date(lang, nxt)) if nxt else ""
                fin = t(lang, "my_waiting") if p.is_finished else ""
                lines.append(f'📺 <a href="{p.url}">{p.title}</a> — {st}{extra}{fin}')
                label = p.title
            voices = await _voices_for_sub(s, sub)
            lines[-1] += t(lang, "my_voice", v=_voice_label(lang, sub, voices))
            rows.append([(f"🎙 {label[:22]}", f"voices:{sub.id}"), ("❌", f"unsub:{sub.id}")])
    return "\n".join(lines), _kb(rows)


@dp.callback_query(F.data.startswith("unsub:"))
async def cb_unsubscribe(cb: CallbackQuery) -> None:
    lang = await _lang(cb.from_user.id)
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"))
        return
    async with session() as s:
        ok = await svc.unsubscribe(s, cb.from_user.id, int(cb.data.split(":")[1]))
        await s.commit()
    await cb.answer(t(lang, "toast_unsubscribed" if ok else "toast_no_sub"))
    text_, kb = await _render_my(cb.from_user.id)
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("unsubq:"))
async def cb_unsubscribe_ask(cb: CallbackQuery) -> None:
    """«🔕 Не следить» в уведомлении: сначала подтверждение — заменяем последний ряд кнопок."""
    lang = await _lang(cb.from_user.id)
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"))
        return
    sub_id = int(cb.data.split(":")[1])
    await _swap_row(cb, [InlineKeyboardButton(text=t(lang, "btn_unsub_yes"), callback_data=f"unsub:{sub_id}"),
                              InlineKeyboardButton(text=t(lang, "btn_keep"), callback_data=f"keep:{sub_id}")])
    await cb.answer()


@dp.callback_query(F.data.startswith("keep:"))
async def cb_keep(cb: CallbackQuery) -> None:
    lang = await _lang(cb.from_user.id)
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(lang, "too_fast"))
        return
    sub_id = int(cb.data.split(":")[1])
    sub = await _own_sub(cb.from_user.id, sub_id)
    label = t(lang, "btn_unfollow_fr" if sub and sub.scope == "franchise" else "btn_unfollow")
    await _swap_row(cb, [InlineKeyboardButton(text=label, callback_data=f"unsubq:{sub_id}")])
    await cb.answer()


async def _swap_row(cb: CallbackQuery, row: list[InlineKeyboardButton]) -> None:
    """Подтверждение подменяет ряд нажатой кнопки. В уведомлении он последний, а в карточке сериала
    ниже ещё расписание и «Поделиться» — по индексу, иначе подтверждение съело бы чужие кнопки."""
    kb = cb.message.reply_markup.inline_keyboard if cb.message and cb.message.reply_markup else []
    idx = next((i for i, r in enumerate(kb) if any(b.callback_data == cb.data for b in r)), len(kb) - 1)
    rows = [row if i == idx else list(r) for i, r in enumerate(kb)] or [row]
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


# ----------------------------------------------------------------------------- 🆕 Новое (§7.6)

NEW_SQL = text("""
    SELECT DISTINCT e.id, p.title, p.url, p.default_translator, e.season, e.episode, e.first_seen_at,
           (SELECT string_agg(v.name, ', ' ORDER BY ev.seen_at)
              FROM episode_voices ev JOIN voices v ON v.page_id = p.id AND v.translator_id = ev.translator_id
             WHERE ev.episode_id = e.id) AS voices,
           (SELECT ev.translator_id FROM episode_voices ev WHERE ev.episode_id = e.id ORDER BY ev.seen_at LIMIT 1)
      FROM subscriptions sub
      JOIN pages p ON (p.id = sub.page_id OR (sub.franchise_id IS NOT NULL AND p.franchise_id = sub.franchise_id))
      JOIN episodes e ON e.page_id = p.id
     WHERE sub.user_id = :uid AND e.first_seen_at > now() - interval '7 days'
     ORDER BY e.first_seen_at DESC LIMIT 30""")
NEW_PARTS_SQL = text("""
    SELECT p.title, f.name FROM notifications n
      JOIN pages p ON p.id = n.ref_id LEFT JOIN franchises f ON f.id = p.franchise_id
     WHERE n.user_id = :uid AND n.kind = 'new_part' AND n.created_at > now() - interval '7 days'
     ORDER BY n.created_at DESC LIMIT 10""")


async def _render_new(user_id: int):
    lang = await _lang(user_id)
    async with session() as s:
        u = await s.get(User, user_id)
        has_subs = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id))
        rows = (await s.execute(NEW_SQL, {"uid": user_id})).all()
        parts = (await s.execute(NEW_PARTS_SQL, {"uid": user_id})).all()
    if not has_subs:
        return t(lang, "new_no_subs"), _kb([])
    if not rows and not parts:
        return t(lang, "new_empty"), _kb([])
    tz = timedelta(hours=u.tz_offset if u else 3)
    lines, cur, buttons = [t(lang, "new_head")], None, []
    for _eid, title, url, default_tid, season, episode, seen, voices, first_tid in rows:
        day = (seen + tz).date()
        if day != cur:
            cur = day
            lines.append(f"\n<b>{when(lang, day)}</b>")
        lines.append(f"  • {html.escape(title[:36])} — {season}×{episode}" + (f" · {html.escape(voices)}" if voices else ""))
        if len(buttons) < 10:
            buttons.append([InlineKeyboardButton(text=fit_button("▶ ", title, f" {season}×{episode}"),
                                                 url=watch_url(url, first_tid or default_tid, season, episode))])
    if parts:
        lines.append(t(lang, "new_parts_head"))
        lines += [f"  • {html.escape(title[:44])}" if not fname or title.startswith(fname)
                  else f"  • {html.escape(fname)}: {html.escape(title[:36])}" for title, fname in parts]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# ----------------------------------------------------------------------------- ⚙️ Настройки (§7.7)

QUIET_DEFAULT = (23, 8)
DIGEST_DEFAULT_HOUR = 20


def _tz_label(lang: str, tz: int) -> str:
    note = {3: "tz_moscow" if lang == "ru" else "tz_kyiv_summer", 2: "tz_kyiv_winter"}.get(tz)
    return f"UTC{tz:+d}" + (f" · {t(lang, note)}" if note else "")


async def _render_settings(user_id: int):
    lang = await _lang(user_id)
    async with session() as s:
        u = await s.get(User, user_id)
        names = dict((await s.execute(text(
            "SELECT DISTINCT ON (translator_id) translator_id, name FROM voices ORDER BY translator_id, name"))).all()) \
            if u.default_voice_filter else {}
    quiet_on = u.quiet_from is not None and u.quiet_to is not None
    quiet = f"{u.quiet_from:02d}:00–{u.quiet_to:02d}:00" if quiet_on else t(lang, "off")
    delivery = t(lang, "delivery_digest", h=u.digest_hour) if u.digest_hour is not None else t(lang, "delivery_now")
    voice = ", ".join(names.get(t_, f"#{t_}") for t_ in u.default_voice_filter) if u.default_voice_filter else t(lang, "voice_any")
    text_ = (t(lang, "set_head") + "\n\n"
             + t(lang, "set_photos", v=t(lang, "on" if u.photos else "off")) + "\n"
             + t(lang, "set_quiet", v=quiet) + (t(lang, "set_quiet_note") if quiet_on else "") + "\n"
             + t(lang, "set_tz", v=_tz_label(lang, u.tz_offset)) + "\n"
             + t(lang, "set_delivery", v=delivery) + "\n"
             + t(lang, "set_voice", v=voice) + "\n"
             + t(lang, "set_lang", v=LANGS.get(u.lang, u.lang)))
    quiet_row = [(t(lang, "btn_quiet_off"), "set:quiet"), (t(lang, "btn_quiet_edit"), "set:quietcfg")] if quiet_on \
        else [(t(lang, "btn_quiet_on", f=QUIET_DEFAULT[0], t=QUIET_DEFAULT[1]), "set:quiet")]
    rows = [[(t(lang, "btn_photos_off" if u.photos else "btn_photos_on"), "set:photos")],
            quiet_row,
            [(t(lang, "btn_tz_minus"), "set:tz:-1"), (_tz_label(lang, u.tz_offset), "noop"), (t(lang, "btn_tz_plus"), "set:tz:1")],
            [(t(lang, "btn_digest_now") if u.digest_hour is not None else t(lang, "btn_digest", h=DIGEST_DEFAULT_HOUR), "set:digest")],
            [(t(lang, "btn_default_voice"), "set:voice"), (t(lang, "btn_lang"), "set:lang")]]
    return text_, _kb(rows)


async def _render_quiet(user_id: int):
    """Границы тихих часов: ± по часу для начала и конца; «выключить» — обратно в настройки."""
    lang = await _lang(user_id)
    async with session() as s:
        u = await s.get(User, user_id)
    f, to = (u.quiet_from, u.quiet_to) if u.quiet_from is not None and u.quiet_to is not None else QUIET_DEFAULT
    rows = [[("−1", "setq:f:-1"), (t(lang, "quiet_from_label", h=f), "noop"), ("+1", "setq:f:1")],
            [("−1", "setq:t:-1"), (t(lang, "quiet_to_label", h=to), "noop"), ("+1", "setq:t:1")],
            [(t(lang, "btn_quiet_disable"), "setq:off")],
            [(t(lang, "btn_done"), "set:back")]]
    return t(lang, "quiet_head", f=f, t=to), _kb(rows)


async def _render_default_voice(user_id: int):
    lang = await _lang(user_id)
    async with session() as s:
        u = await s.get(User, user_id)
        top = (await s.execute(text("SELECT translator_id, min(name), count(*) AS c FROM voices "
                                    "GROUP BY translator_id ORDER BY c DESC LIMIT 15"))).all()
    chosen = set(u.default_voice_filter or [])
    rows = [[(("☑ " if not chosen else "☐ ") + t(lang, "btn_any"), "setv:any")]]
    rows += [[(f"{'☑' if tid in chosen else '☐'} {name[:30]}", f"setv:{tid}")] for tid, name, _ in top]
    rows.append([(t(lang, "btn_done"), "set:back")])
    return t(lang, "dv_head"), _kb(rows)


async def _render_lang(user_id: int):
    lang = await _lang(user_id)
    rows = [[(("☑ " if code == lang else "☐ ") + name, f"setl:{code}")] for code, name in LANGS.items()]
    rows.append([(t(lang, "btn_done"), "set:back")])
    return t(lang, "lang_head"), _kb(rows)


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    parts = cb.data.split(":")
    what = parts[1]
    async with session() as s:
        await _touch_user(s, cb.from_user)
        u = await s.get(User, cb.from_user.id)
        if what == "photos":
            u.photos = not u.photos
        elif what == "quiet":
            u.quiet_from, u.quiet_to = (None, None) if u.quiet_from is not None else QUIET_DEFAULT
        elif what == "tz":
            u.tz_offset = max(-12, min(14, u.tz_offset + int(parts[2])))
        elif what == "digest":
            u.digest_hour = None if u.digest_hour is not None else DIGEST_DEFAULT_HOUR
        if what in ("quiet", "tz", "digest"):
            await s.flush()
            await svc.reschedule_pending(s, cb.from_user.id)   # уже стоящие в очереди — по новым правилам
        await s.commit()
    render = {"voice": _render_default_voice, "lang": _render_lang, "quietcfg": _render_quiet}.get(what, _render_settings)
    text_, kb = await render(cb.from_user.id)
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("setq:"))
async def cb_quiet(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    parts = cb.data.split(":")
    async with session() as s:
        await _touch_user(s, cb.from_user)
        u = await s.get(User, cb.from_user.id)
        if parts[1] == "off":
            u.quiet_from = u.quiet_to = None
        else:
            f, to = (u.quiet_from, u.quiet_to) if u.quiet_from is not None and u.quiet_to is not None else QUIET_DEFAULT
            delta = int(parts[2])
            if parts[1] == "f":
                f = (f + delta) % 24
            else:
                to = (to + delta) % 24
            if f == to:                                     # пустой интервал = тихих часов нет; шагаем дальше
                to = (to + delta) % 24 if parts[1] == "t" else to
                f = (f + delta) % 24 if parts[1] == "f" else f
            u.quiet_from, u.quiet_to = f, to
        await s.flush()
        await svc.reschedule_pending(s, cb.from_user.id)
        await s.commit()
    text_, kb = await (_render_settings if parts[1] == "off" else _render_quiet)(cb.from_user.id)
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("setl:"))
async def cb_lang(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    code = cb.data.split(":")[1]
    if code not in LANGS:
        await cb.answer()
        return
    async with session() as s:
        await _touch_user(s, cb.from_user)
        u = await s.get(User, cb.from_user.id)
        changed = u.lang != code
        u.lang = code
        await s.commit()
    _langs[cb.from_user.id] = code
    await cb.answer()
    text_, kb = await _render_settings(cb.from_user.id)
    await _edit(cb, text_, kb)
    if changed:   # нижняя клавиатура меняется только новым сообщением
        await cb.message.answer(t(code, "lang_switched"), reply_markup=menu(code))


@dp.callback_query(F.data.startswith("setv:"))
async def cb_default_voice(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(t(await _lang(cb.from_user.id), "too_fast"))
        return
    val = cb.data.split(":")[1]
    async with session() as s:
        await _touch_user(s, cb.from_user)
        u = await s.get(User, cb.from_user.id)
        if val == "any":
            u.default_voice_filter = None
        else:
            u.default_voice_filter = sorted(set(u.default_voice_filter or []) ^ {int(val)}) or None
        await s.commit()
    text_, kb = await _render_default_voice(cb.from_user.id)
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@dp.callback_query()
async def cb_unknown(cb: CallbackQuery) -> None:
    await cb.answer()   # чужие/устаревшие callback_data — молча


async def _edit(cb: CallbackQuery, text_: str, kb: InlineKeyboardMarkup) -> None:
    await _edit_message(cb.message, text_, kb)


async def _edit_message(m: Message, text_: str, kb: InlineKeyboardMarkup) -> None:
    """Нажатие всегда меняет сообщение (§7.11). Карточка с постером — это фото: правим подпись;
    длинный текст в подпись не влезает — тогда новым сообщением."""
    try:
        if m.photo:
            if len(text_) > posters.CAPTION_MAX_LEN:
                await m.answer(text_, reply_markup=kb, disable_web_page_preview=True)
            else:
                await m.edit_caption(caption=text_, reply_markup=kb)
        else:
            await m.edit_text(text_, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            await m.answer(text_, reply_markup=kb, disable_web_page_preview=True)


@dp.errors()
async def on_error(event: ErrorEvent) -> None:
    """Любая необработанная ошибка — в лог; бот продолжает работать."""
    upd = event.update
    if isinstance(event.exception, TelegramBadRequest) and "query is too old" in str(event.exception):
        # Нажатие из бэклога (во время перезапуска): Telegram уже не принимает ответ на него.
        log.info("Просроченный callback %s — пропускаю", upd.callback_query.data if upd.callback_query else "?")
        return
    log.exception("Ошибка обработчика: %s", event.exception)
    try:
        if upd.message:
            lang = _langs.get(upd.message.from_user.id, "ru") if upd.message.from_user else "ru"
            await upd.message.answer(t(lang, "error_msg"))
        elif upd.callback_query:
            lang = _langs.get(upd.callback_query.from_user.id, "ru")
            await upd.callback_query.answer(t(lang, "error_cb"), show_alert=False)
    except Exception:
        pass


# ----------------------------------------------------------------------------- запуск

async def main() -> None:
    global bot
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username or BOT_USERNAME
    # Список команд — на языке клиента Telegram (без language_code — для всех остальных, по-русски).
    for lang in LANGS:
        await bot.set_my_commands([
            BotCommand(command="my", description=t(lang, "cmd_my")),
            BotCommand(command="new", description=t(lang, "cmd_new")),
            BotCommand(command="calendar", description=t(lang, "cmd_calendar")),
            BotCommand(command="settings", description=t(lang, "cmd_settings")),
            BotCommand(command="help", description=t(lang, "cmd_help")),
        ], language_code=None if lang == "ru" else lang)
    log.info("Бот запущен")
    try:
        # Бэклог за время перезапуска сохраняем (устаревшие сообщения отсеет SkipStaleUpdates);
        # принимаем только нужные типы апдейтов.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
