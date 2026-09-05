"""Телеграм-бот: поиск, подписки на страницу или франшизу, выбор озвучки, /my.

Поведение — docs/ARCHITECTURE.md, §6; защита — app/bot/guard.py.
Бот ходит на сайт только по действию пользователя и через тот же клиент
с паузами, что и поллер. В Telegram уходят только ответы пользователю.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)
from sqlalchemy import func, select, text

from app import posters
from app import service as svc
from app.bot import guard
from app.bot.search import group_hits
from app.config import cfg
from app.db import init_db, session
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
_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
SECTION_LABEL = {"series": "сериал", "animation": "аниме", "cartoons": "мультсериал", "films": "фильм"}

BTN_FIND, BTN_MY, BTN_NEW = "🔍 Найти", "📋 Мои подписки", "🆕 Новое"
BTN_CAL, BTN_SETTINGS, BTN_HELP = "📅 Календарь", "⚙️ Настройки", "❓ Помощь"
MAIN_MENU = ReplyKeyboardMarkup(   # 3×2, docs/PRODUCT_AND_SCALE.md §7.1
    keyboard=[[KeyboardButton(text=BTN_FIND), KeyboardButton(text=BTN_MY)],
              [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_CAL)],
              [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True, is_persistent=True,
)
HINT = "\n\n<i>Добавить ещё — напишите название. Список — «📋 Мои подписки».</i>"
START = (
    "Привет! Я сообщу, когда выйдет новая серия.\n\n"
    "Напишите название — например, «слизь» — и выберите, за чем следить. Можно за одним сезоном, "
    "а можно за всей франшизой: тогда расскажу и о новых сезонах, фильмах, спин-оффах.\n\n"
    "Кнопки внизу — всё управление. 👇"
)
HELP = (
    "Слежу за выходом новых серий на HDREZKA и присылаю уведомления.\n\n"
    "<b>Как подписаться</b>\n"
    "• пришлите название — покажу, что сейчас выходит\n"
    "• или ссылку на страницу тайтла\n\n"
    "Подписаться можно на <b>один сезон</b> или на <b>всю франшизу</b> — тогда "
    "сообщу и о новых сезонах, фильмах и спин-оффах.\n\n"
    "Кнопки внизу — главное меню: поиск, подписки, что нового за неделю, календарь, настройки, помощь."
)
BUSY = "Сайт сейчас отвечает медленно или недоступен — попробуйте через пару минут."
BOT_USERNAME = "HDRezkaSeriesBot"   # уточняется при старте через get_me()
TOO_FAST = "Слишком много запросов — подождите минуту."

_cards: dict[int, tuple[FeedItem, float]] = {}
_search_cache: dict[str, tuple[list[FeedItem], float]] = {}

# Только личные чаты. Группы, каналы, другие боты — молча игнорируем.
dp.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.is_bot == False)  # noqa: E712
dp.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """(текст, callback_data) или (текст, https-ссылка)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=d) if d.startswith("https://") else InlineKeyboardButton(text=t, callback_data=d)
         for t, d in row] for row in rows])


def _share_url(start_arg: str, title: str) -> str:
    """§7.10: кнопка открывает диалог «поделиться» с deep-link на карточку — друг подписывается в одно нажатие."""
    link = f"https://t.me/{BOT_USERNAME}?start={start_arg}"
    return "https://t.me/share/url?" + urlencode({"url": link, "text": f"Следить за «{title[:60]}» — новые серии в Telegram"})


_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
CAL_DAYS = 14
CAL_NOTE = "\n\n<i>Даты — оригинального эфира. На HDREZKA серия появляется позже, обычно в тот же день или на следующий.</i>"


def _ru_date(d: date | None) -> str | None:
    return f"{d.day} {_MONTHS[d.month - 1]}" if d else None


def _when(d: date) -> str:
    delta = (d - date.today()).days
    if delta == 0:
        rel = "сегодня"
    elif delta == 1:
        rel = "завтра"
    elif delta == -1:
        rel = "вчера"
    elif delta > 1:
        rel = f"через {delta} дн."
    else:
        rel = f"{-delta} дн. назад"
    return f"{_WEEKDAYS[d.weekday()]} {_ru_date(d)} · {rel}"


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
        await svc.upsert_user(s, msg.from_user.id, msg.from_user.username)
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
            await msg.answer("Эту страницу я пока не знаю. Напишите название — найду.", reply_markup=MAIN_MENU)
        elif target[0] == "page":
            await msg.answer("Карточка по ссылке 👇", reply_markup=MAIN_MENU)
            await _send_card(msg, msg.from_user.id, target[1])
        else:
            text_, kb = await _render_franchise_overview(msg.from_user.id, target[1], None)
            await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)
        return
    await msg.answer(START, reply_markup=MAIN_MENU)


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP, reply_markup=MAIN_MENU)


@dp.message(Command("my"))
@dp.message(F.text == BTN_MY)
async def cmd_my(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    text_, kb = await _render_my(msg.from_user.id)
    await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)


@dp.message(F.text == BTN_FIND)
async def btn_find(msg: Message) -> None:
    await msg.answer("Напишите название: например, <i>слизь</i> или <i>дом дракона</i>. "
                     "Можно прислать ссылку на страницу HDREZKA.", reply_markup=MAIN_MENU)


@dp.message(Command("new"))
@dp.message(F.text == BTN_NEW)
async def cmd_new(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    text_, kb = await _render_new(msg.from_user.id)
    await msg.answer(text_, reply_markup=kb, disable_web_page_preview=True)


@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def cmd_settings(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    async with session() as s:
        await svc.upsert_user(s, msg.from_user.id, msg.from_user.username)
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
        frs = await s.scalar(select(func.count()).select_from(Franchise))
        pending = await s.scalar(text("SELECT count(*) FROM notifications WHERE status = 'pending'"))
        sent = await s.scalar(text("SELECT count(*) FROM notifications WHERE status = 'sent'"))
        last = await svc.meta_get(s, "last_poll_ok")
        stale = await svc.meta_get(s, "poller_stale") == "1"
    await msg.answer(
        ("⚠️ Поллер молчит дольше порога — проверьте туннель и логи\n" if stale else "")
        + f"Пользователей: {users} (активных {active})\n"
        f"Подписок: на страницы {sp}, на франшизы {sf}\n"
        f"Страниц в базе: {pages}, франшиз: {frs}\n"
        f"Уведомлений: в очереди {pending}, отправлено (7 дн.) {sent}\n"
        f"Последний обход: {last}")


# ----------------------------------------------------------------------------- текст: ссылка или поиск

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    query = guard.clean_query(msg.text)
    if len(query) < 2:
        await msg.answer("Напишите название хотя бы из двух букв — например «слизь» или «дом дракона». "
                         "Или пришлите ссылку на страницу HDREZKA.", reply_markup=MAIN_MENU)
        return
    link = _PATH_RX.search(msg.text)
    limiter = guard.site_actions if link else guard.cheap_actions   # локальный поиск сайт не трогает
    if not limiter.allow(msg.from_user.id):
        await msg.answer(TOO_FAST)
        return
    async with session() as s:
        await svc.upsert_user(s, msg.from_user.id, msg.from_user.username)
        await s.commit()
    if link:
        await _handle_link(msg, int(link.group(2)), link.group(1))
    else:
        await _handle_search(msg, query)


@dp.message()
async def on_other(msg: Message) -> None:
    """Стикеры, фото, голосовые — подсказываем, что бот понимает только текст."""
    if guard.cheap_actions.allow(msg.from_user.id):
        await msg.answer("Я понимаю только текст: название сериала или ссылку на страницу HDREZKA.",
                         reply_markup=MAIN_MENU)


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


async def _handle_search(msg: Message, query: str) -> None:
    """Сначала каталог (миллисекунды, без сайта); сайт — когда каталог не знает или по кнопке."""
    async with session() as s:
        g = group_hits(await svc.search_catalog(s, query))
        stats = await svc.franchise_stats(s, g.franchise_ids) if g.franchise_ids else {}
    if g.empty:
        if not guard.site_actions.allow(msg.from_user.id):
            await msg.answer(TOO_FAST)
            return
        note = await msg.answer("Ищу…")
        await _site_search(note, query, local_hidden=g.hidden)
        return
    rows = []
    for fid in g.franchise_ids:
        name, parts, ongoing = stats.get(fid, ("франшиза", 0, 0))
        tail = f"{parts} частей" + (f", выходят {ongoing}" if ongoing else ", ничего не выходит")
        rows.append([(f"🎞 {name[:22]} · {tail}", f"subf:{fid}:{g.origin[fid]}")])
    rows += [[(f"➕ {p.title[:34]} · {SECTION_LABEL.get(p.section or '', '')} · {p.last_season}×{p.last_episode}",
               f"sub:{p.hdrezka_id}")] for p in g.standalone]
    rows.append([("🔍 Искать на сайте", f"site:{_site_token(query)}")])
    text_ = f"Нашёл ({len(rows) - 1}). Выберите:"
    if g.hidden:
        text_ += f"\n<i>Скрыто {g.hidden}: завершённые сезоны и фильмы.</i>"
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
    hit = _site_queries.get(cb.data.split(":", 1)[1])
    if not hit or time.time() - hit[1] > SEARCH_TTL * 6:
        await cb.answer("Запрос устарел — напишите название ещё раз.", show_alert=True)
        return
    if not guard.site_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    await cb.answer()
    note = await cb.message.answer("Ищу на сайте…")
    await _site_search(note, hit[0])


async def _site_search(note: Message, query: str, local_hidden: int = 0) -> None:
    try:
        items = await _search(query)
    except (AccessBlocked, asyncio.TimeoutError):
        await note.edit_text(BUSY)
        return
    _remember(items)
    # Каталог-first: все карточки ответа — в pages (страницы дочитает очередь поллера).
    # База учится на пользователях: следующий такой поиск ответит без сайта.
    finished_known: set[int] = set()
    franchises: list = []
    async with session() as s:
        known = [i.hdrezka_id for i in items]
        for i in items:
            await svc.upsert_page_from_feed(s, i)
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
    fr_rows = [[(f"🎞 Франшиза «{name[:28]}»", f"subf:{fid}:{pid}")] for fid, name, pid in franchises]
    if not ongoing:
        hidden = local_hidden + sum(1 for i in items if i.is_finished or i.looks_like_film
                                    or i.hdrezka_id in finished_known)
        text_ = ("Сейчас ничего выходящего по этому запросу нет."
                 + (f" Скрыто {hidden}: завершённые сезоны и фильмы — на них подписаться нельзя." if hidden else ""))
        if fr_rows:
            text_ += "\n\nЕсть франшиза: подпишитесь на неё — сообщу о новых сезонах, фильмах, спин-оффах."
        else:
            text_ += ("\n\nЕсли у тайтла есть франшиза — пришлите ссылку на любую его страницу, "
                      "предложу подписку на всю франшизу.")
        await note.edit_text(text_, reply_markup=_kb(fr_rows) if fr_rows else None)
        return
    # Каждая кнопка понятна без контекста: название · раздел · текущая серия.
    rows = [[(f"➕ {i.title[:34]} · {SECTION_LABEL.get(i.section, i.section or '')} · {i.season}×{i.episode}",
              f"sub:{i.hdrezka_id}")] for i in ongoing]
    await note.edit_text(f"Сейчас выходит ({len(ongoing)}). Выберите:", reply_markup=_kb(rows + fr_rows))


PAGE_FRESH_DAYS = 7   # страница в базе моложе — на сайт не ходим: бот учится на пользователях


async def _handle_link(msg: Message, hdrezka_id: int, path: str) -> None:
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
    note = await msg.answer("Читаю страницу…")
    try:
        async with session() as s:
            res = await _site(svc.sync_page(s, client, hdrezka_id, url))
            await s.commit()
            page_id, title = res.page.id, res.page.title
    except (AccessBlocked, asyncio.TimeoutError):
        await note.edit_text(BUSY)
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

    kind = SECTION_LABEL.get(page.section or "", "")
    if page.content_type == "film":
        kind = "фильм"
    head = "✅ Подписал:" if (sub and just_created) else ("В подписках:" if sub else "Найдено:")
    lines = [f"{head} <b>{page.title}</b>" + (f" · {kind}" if kind else "")]
    if page.content_type != "film" and page.last_season:
        if page.is_finished:
            lines.append(f"Последняя серия: {page.last_season}×{page.last_episode} · <b>сериал завершён</b>")
        else:
            lines.append(f"Сейчас: {page.last_season} сезон, {page.last_episode} серия")
    if nxt:
        lines.append(f"Следующая серия: {_ru_date(nxt)}")
    rows = []
    if sub:
        lines.append(f"Озвучка: {_voice_label(sub, voices)}")
        rows.append([("🎙 Выбрать озвучку", f"voices:{sub.id}")])
        rows.append([("❌ Отписаться", f"unsub:{sub.id}")])
    elif fr_sub:
        lines.append(f"Уже входит в вашу подписку на франшизу «{fr.name}».")
    elif page.content_type == "film":
        lines.append("Это фильм — на него подписаться нельзя, новых серий не будет.")
    elif page.is_finished:
        lines.append("Сезон вышел целиком — подписаться на него нельзя.")
    else:
        rows.append([("➕ Подписаться на этот сезон", f"sub:{page.hdrezka_id}")])
    if fr and parts > 1 and not fr_sub:
        rows.append([(f"🎞 Вся франшиза «{fr.name[:24]}» ({parts})", f"subf:{fr.id}:{page.id}")])
    if page.content_type != "film":
        rows.append([("📅 Расписание серий", f"sched:p:{page.id}"), ("▶ Открыть на сайте", page.url)])
    else:
        rows.append([("▶ Открыть на сайте", page.url)])
    rows.append([("🔗 Поделиться", _share_url(f"p_{page.hdrezka_id}", page.title))])
    return "\n".join(lines) + HINT, _kb(rows)


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


def _voice_label(sub: Subscription | None, voices: list[Voice]) -> str:
    if not sub or not sub.voice_filter:
        return f"любая ({len(voices)} доступно)" if voices else "любая"
    names = {v.translator_id: v.name for v in voices}
    return ", ".join(names.get(t, f"#{t}") for t in sub.voice_filter)


async def _too_many_subs(user_id: int) -> bool:
    async with session() as s:
        n = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id))
    return n >= guard.MAX_SUBSCRIPTIONS


@dp.callback_query(F.data.startswith("sub:"))
async def cb_subscribe(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST, show_alert=False)
        return
    hdrezka_id = int(cb.data.split(":")[1])
    if await _too_many_subs(cb.from_user.id):
        await cb.answer(f"Не больше {guard.MAX_SUBSCRIPTIONS} подписок.", show_alert=True)
        return
    async with session() as s:
        await svc.upsert_user(s, cb.from_user.id, cb.from_user.username)
        page = await svc.page_by_hid(s, hdrezka_id)
        if page is None:
            card = _cards.get(hdrezka_id)
            if not card:
                await s.commit()
                await cb.answer("Карточка устарела — повторите поиск.", show_alert=True)
                return
            page = await svc.upsert_page_from_feed(s, card[0])
        if page.content_type == "film" or page.is_finished:
            await s.commit()
            await cb.answer("На это подписаться нельзя: фильм или завершённый сезон.", show_alert=True)
            return
        created = await svc.subscribe_page(s, cb.from_user.id, page.id)
        await s.commit()
        pid, hid, url, fresh = page.id, page.hdrezka_id, page.url, page.page_refreshed_at
    await cb.answer()

    # В выдаче поиска кнопка становится «✓ …» — результат виден без тоста.
    if cb.message and cb.message.reply_markup and cb.message.reply_markup.inline_keyboard:
        tapped = any(b.callback_data == cb.data and b.text.startswith("➕")
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

def _part_status(p: Page) -> tuple[str, bool]:
    """Что известно о части: (подпись, можно ли подписаться на неё отдельно)."""
    kind = "фильм" if p.content_type == "film" else SECTION_LABEL.get(p.section or "", "сериал" if p.content_type else "")
    if p.content_type == "film":
        return f"{kind}", False
    if p.is_finished:
        return f"{kind} · завершён" + (f", {p.last_season}×{p.last_episode}" if p.last_episode else ""), False
    if p.last_episode:
        return f"{kind} · идёт, {p.last_season}×{p.last_episode}", True
    return (kind + " · " if kind else "") + "данных пока нет", False


async def _render_franchise_overview(user_id: int, fid: int, origin_page_id: int | None):
    async with session() as s:
        fr = await s.get(Franchise, fid)
        if fr is None:
            return "Франшиза не найдена.", _kb([])
        sub = await s.scalar(select(Subscription.id).where(Subscription.user_id == user_id,
                                                           Subscription.franchise_id == fid))
        parts = (await s.execute(select(Page).where(Page.franchise_id == fid)
                                 .order_by(Page.year.desc().nulls_last(), Page.id.desc()))).scalars().all()
        my_pages = set((await s.execute(select(Subscription.page_id).where(
            Subscription.user_id == user_id, Subscription.page_id.isnot(None)))).scalars())
    ongoing = [p for p in parts if _part_status(p)[1]]
    rest = [p for p in parts if not _part_status(p)[1]]
    lines = [f"🎞 Франшиза <b>{fr.name}</b> — {len(parts)} частей"]
    if sub:
        lines.append("✅ Вы подписаны на всю франшизу.")
    if ongoing:
        lines.append("\n<b>Сейчас выходят:</b>")
        lines += [f"  • {p.title[:40]} · {_part_status(p)[0]}" + (" ✓" if p.id in my_pages else "") for p in ongoing]
    if rest:
        lines.append("\n<b>Остальные части:</b>")
        lines += [f"  • {p.title[:40]} · {p.year or ''} · {_part_status(p)[0]}" for p in rest[:12]]
        if len(rest) > 12:
            lines.append(f"  … и ещё {len(rest) - 12}")
    lines.append("\nПодписка на всю франшизу — это все выходящие сезоны плюс сообщения о новых частях: "
                 "сезонах, фильмах, спин-оффах. Или выберите отдельные части ниже.")
    rows = []
    if not sub:
        rows.append([("✅ Подписаться на всю франшизу", f"subf_all:{fid}")])
    for p in ongoing[:6]:
        if p.id not in my_pages and not sub:
            rows.append([(f"➕ {p.title[:36]} · {p.last_season}×{p.last_episode}", f"sub:{p.hdrezka_id}")])
    if sub:
        rows.append([("📋 Карточка франшизы", f"fcard:{fid}")])
    rows.append([("🔗 Поделиться", _share_url(f"f_{fr.key_hdrezka_id}", fr.name))])
    if origin_page_id:
        rows.append([("« Назад", f"pcard:{origin_page_id}")])
    return "\n".join(lines), _kb(rows)


@dp.callback_query(F.data.startswith("subf:"))
async def cb_franchise_overview(cb: CallbackQuery) -> None:
    """«Вся франшиза» сначала показывает состав, подписка — отдельным нажатием."""
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    parts = cb.data.split(":")
    fid = int(parts[1])
    origin = int(parts[2]) if len(parts) > 2 else None
    text_, kb = await _render_franchise_overview(cb.from_user.id, fid, origin)
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("subf_all:"))
async def cb_subscribe_franchise(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    fid = int(cb.data.split(":")[1])
    if await _too_many_subs(cb.from_user.id):
        await cb.answer(f"Не больше {guard.MAX_SUBSCRIPTIONS} подписок.", show_alert=True)
        return
    async with session() as s:
        await svc.upsert_user(s, cb.from_user.id, cb.from_user.username)
        if await s.get(Franchise, fid) is None:
            await cb.answer("Франшиза не найдена.", show_alert=True)
            return
        created = await svc.subscribe_franchise(s, cb.from_user.id, fid)
        await s.commit()
    await cb.answer()
    text_, kb = await _render_franchise_card(cb.from_user.id, fid, created)
    await _edit(cb, text_, kb)


async def _render_franchise_card(user_id: int, fid: int, created: bool = True):
    async with session() as s:
        fr = await s.get(Franchise, fid)
        sub = await s.scalar(select(Subscription).where(Subscription.user_id == user_id,
                                                        Subscription.franchise_id == fid))
        parts = (await s.execute(select(Page).where(Page.franchise_id == fid)
                                 .order_by(Page.year.desc().nulls_last(), Page.id.desc()))).scalars().all()
        voices = (await s.execute(select(Voice).join(Page, Page.id == Voice.page_id)
                                  .where(Page.franchise_id == fid))).scalars().all()
    ongoing = [p for p in parts if not p.is_finished and p.last_episode and p.content_type != "film"]
    head = "✅ Подписал на франшизу" if (sub and created) else ("В подписках — франшиза" if sub else "Франшиза")
    lines = [f"{head} <b>{fr.name}</b> ({len(parts)} частей)"]
    if ongoing:
        lines.append("Сейчас выходят:")
        lines += [f"  • {p.title[:44]} — {p.last_season}×{p.last_episode}" for p in ongoing[:5]]
    lines.append("Сообщу о новых сериях, сезонах, фильмах и спин-оффах.")
    uniq = list({v.translator_id: v for v in voices}.values())
    rows = []
    if sub:
        lines.append(f"Озвучка: {_voice_label(sub, uniq)}")
        rows.append([("🎙 Выбрать озвучку", f"voices:{sub.id}")])
        rows.append([("❌ Отписаться", f"unsub:{sub.id}")])
    else:
        rows.append([("➕ Подписаться на франшизу", f"subf_all:{fid}")])
    rows.append([("📅 Расписание серий", f"sched:f:{fid}")])
    rows.append([("🎞 Состав франшизы", f"subf:{fid}")])
    rows.append([("🔗 Поделиться", _share_url(f"f_{fr.key_hdrezka_id}", fr.name))])
    return "\n".join(lines) + HINT, _kb(rows)


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


def _fmt_sched(rows, with_title: bool) -> list[str]:
    out = []
    for _pid, title, season, episode, d, aired in rows:
        mark = "✓" if aired or d < date.today() else "•"
        who = f"<b>{title[:30]}</b> " if with_title else ""
        out.append(f"{mark} {who}{season}×{episode} — {_when(d)}")
    return out


async def _render_schedule(kind: str, obj_id: int):
    async with session() as s:
        if kind == "p":
            page = await s.get(Page, obj_id)
            head = f"📅 <b>{page.title}</b>" if page else "📅"
            rows = (await s.execute(text(SCHED_TEMPLATE.format(where="p.id = :id")), {"id": obj_id})).all()
            back = ("« К сериалу", f"pcard:{obj_id}")
        else:
            fr = await s.get(Franchise, obj_id)
            head = f"📅 Франшиза <b>{fr.name}</b>" if fr else "📅"
            rows = (await s.execute(text(SCHED_TEMPLATE.format(where="p.franchise_id = :id")), {"id": obj_id})).all()
            back = ("« К франшизе", f"fcard:{obj_id}")
    lines = _fmt_sched(rows, with_title=(kind == "f"))
    body = "\n".join(lines) if lines else "Дат ближайших серий нет — либо сезон вышел целиком, либо сайт указывает только год."
    return f"{head}\n\n{body}{CAL_NOTE}", _kb([[back]])


@dp.callback_query(F.data.startswith("sched:"))
async def cb_schedule(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    _, kind, obj_id = cb.data.split(":")
    text_, kb = await _render_schedule(kind, int(obj_id))
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
@dp.message(F.text == BTN_CAL)
async def cmd_calendar(msg: Message) -> None:
    if not guard.cheap_actions.allow(msg.from_user.id):
        return
    async with session() as s:
        rows = (await s.execute(CALENDAR_SQL, {"uid": msg.from_user.id, "days": CAL_DAYS})).all()
        has_subs = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == msg.from_user.id))
    if not has_subs:
        await msg.answer("Подписок пока нет — календарь пуст. Напишите название сериала.", reply_markup=MAIN_MENU)
        return
    if not rows:
        await msg.answer(f"В ближайшие {CAL_DAYS} дней по вашим подпискам серий не запланировано — "
                         "или сайт не указывает точных дат (у части сериалов есть только год)." + CAL_NOTE,
                         reply_markup=MAIN_MENU)
        return
    lines, cur = [f"📅 <b>Ближайшие {CAL_DAYS} дней</b>"], None
    for title, season, episode, d in rows:
        if d != cur:
            cur = d
            lines.append(f"\n<b>{_when(d)}</b>")
        lines.append(f"  • {title[:36]} — {season}×{episode}")
    await msg.answer("\n".join(lines) + CAL_NOTE, reply_markup=MAIN_MENU)


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
    sub = await _own_sub(user_id, sub_id)
    if not sub:
        return "Подписка не найдена.", _kb([])
    async with session() as s:
        voices = await _voices_for_sub(s, sub)
    chosen = set(sub.voice_filter or [])
    rows = [[("☑ Любая озвучка" if not chosen else "☐ Любая озвучка", f"vany:{sub_id}")]]
    rows += [[(f"{'☑' if v.translator_id in chosen else '☐'} {v.name[:36]}", f"vt:{sub_id}:{v.translator_id}")]
             for v in voices[:40]]
    rows.append([("« Готово", f"card:{sub_id}")])
    hint = ("Отмечайте нужные озвучки — уведомлю, когда серия появится именно в них.\n"
            "«Любая» — сообщаю при первом появлении серии.")
    if not voices:
        hint = "Озвучек пока не знаю — страница ещё не прочитана. Попробуйте позже."
    return hint, _kb(rows)


@dp.callback_query(F.data.startswith("voices:"))
async def cb_voices(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    text_, kb = await _render_voices(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("vt:"))
async def cb_voice_toggle(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
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
    await cb.answer("Любая озвучка")
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("card:"))
async def cb_card(cb: CallbackQuery) -> None:
    sub = await _own_sub(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer()
    if not sub:
        await _edit(cb, "Подписка не найдена.", _kb([]))
        return
    if sub.page_id:
        text_, kb = await _render_page_card(cb.from_user.id, sub.page_id)
    else:
        text_, kb = await _render_franchise_card(cb.from_user.id, sub.franchise_id, created=False)
    await _edit(cb, text_, kb)


# ----------------------------------------------------------------------------- /my и отписка

async def _render_my(user_id: int):
    async with session() as s:
        subs = (await s.execute(select(Subscription).where(Subscription.user_id == user_id)
                                .order_by(Subscription.scope, Subscription.created_at))).scalars().all()
        if not subs:
            return "Подписок пока нет. Напишите название сериала или пришлите ссылку на тайтл.", _kb([])
        lines, rows = [f"<b>Ваши подписки ({len(subs)}):</b>"], []
        for sub in subs:
            if sub.scope == "franchise":
                fr = await s.get(Franchise, sub.franchise_id)
                n = await s.scalar(select(func.count()).select_from(Page).where(Page.franchise_id == fr.id))
                ongoing = (await s.execute(select(Page).where(Page.franchise_id == fr.id, Page.is_finished.is_(False),
                                                              Page.last_episode.isnot(None), Page.content_type != "film"))).scalars().all()
                tail = "; ".join(f"{p.title[:28]} {p.last_season}×{p.last_episode}" for p in ongoing[:2])
                lines.append(f"🎞 <b>{fr.name}</b> — франшиза, {n} частей" + (f"\n    выходит: {tail}" if tail else ""))
                label = fr.name
            else:
                p = await s.get(Page, sub.page_id)
                nxt = await s.scalar(select(func.min(Schedule.air_date)).where(
                    Schedule.page_id == p.id, Schedule.aired.is_(False), Schedule.air_date >= date.today()))
                st = f"{p.last_season}×{p.last_episode}" if p.last_episode else "—"
                extra = f" · след. {_ru_date(nxt)}" if nxt else ""
                fin = " · завершён" if p.is_finished else ""
                lines.append(f'📺 <a href="{p.url}">{p.title}</a> — {st}{extra}{fin}')
                label = p.title
            voices = await _voices_for_sub(s, sub)
            lines[-1] += f"\n    озвучка: {_voice_label(sub, voices)}"
            rows.append([(f"🎙 {label[:22]}", f"voices:{sub.id}"), ("❌", f"unsub:{sub.id}")])
    return "\n".join(lines), _kb(rows)


@dp.callback_query(F.data.startswith("unsub:"))
async def cb_unsubscribe(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    async with session() as s:
        ok = await svc.unsubscribe(s, cb.from_user.id, int(cb.data.split(":")[1]))
        await s.commit()
    await cb.answer("Отписал" if ok else "Подписки уже нет")
    text_, kb = await _render_my(cb.from_user.id)
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("unsubq:"))
async def cb_unsubscribe_ask(cb: CallbackQuery) -> None:
    """«🔕 Не следить» в уведомлении: сначала подтверждение — заменяем последний ряд кнопок."""
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    sub_id = int(cb.data.split(":")[1])
    await _swap_last_row(cb, [InlineKeyboardButton(text="🔕 Да, отписаться", callback_data=f"unsub:{sub_id}"),
                              InlineKeyboardButton(text="Оставить", callback_data=f"keep:{sub_id}")])
    await cb.answer()


@dp.callback_query(F.data.startswith("keep:"))
async def cb_keep(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    sub_id = int(cb.data.split(":")[1])
    sub = await _own_sub(cb.from_user.id, sub_id)
    label = "🔕 Не следить за франшизой" if sub and sub.scope == "franchise" else "🔕 Не следить"
    await _swap_last_row(cb, [InlineKeyboardButton(text=label, callback_data=f"unsubq:{sub_id}")])
    await cb.answer()


async def _swap_last_row(cb: CallbackQuery, row: list[InlineKeyboardButton]) -> None:
    kb = cb.message.reply_markup.inline_keyboard if cb.message and cb.message.reply_markup else []
    rows = list(kb[:-1]) + [row]
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
    async with session() as s:
        u = await s.get(User, user_id)
        has_subs = await s.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id))
        rows = (await s.execute(NEW_SQL, {"uid": user_id})).all()
        parts = (await s.execute(NEW_PARTS_SQL, {"uid": user_id})).all()
    if not has_subs:
        return "Подписок пока нет. Напишите название — например, <i>слизь</i>.", _kb([])
    if not rows and not parts:
        return "За неделю новых серий по вашим подпискам не было.", _kb([])
    tz = timedelta(hours=u.tz_offset if u else 3)
    lines, cur, buttons = ["🆕 <b>За неделю</b>"], None, []
    for _eid, title, url, default_tid, season, episode, seen, voices, first_tid in rows:
        day = (seen + tz).date()
        if day != cur:
            cur = day
            lines.append(f"\n<b>{_when(day)}</b>")
        lines.append(f"  • {html.escape(title[:36])} — {season}×{episode}" + (f" · {html.escape(voices)}" if voices else ""))
        if len(buttons) < 10:
            buttons.append([InlineKeyboardButton(text=fit_button("▶ ", title, f" {season}×{episode}"),
                                                 url=watch_url(url, first_tid or default_tid, season, episode))])
    if parts:
        lines.append("\n<b>Новые части франшиз</b>")
        lines += [f"  • {html.escape(title[:44])}" if not fname or title.startswith(fname)
                  else f"  • {html.escape(fname)}: {html.escape(title[:36])}" for title, fname in parts]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# ----------------------------------------------------------------------------- ⚙️ Настройки (§7.7)

QUIET_DEFAULT = (23, 8)
DIGEST_DEFAULT_HOUR = 20


def _tz_label(tz: int) -> str:
    return f"UTC{tz:+d}" + (" · Москва" if tz == 3 else "")


async def _render_settings(user_id: int):
    async with session() as s:
        u = await s.get(User, user_id)
        names = dict((await s.execute(text(
            "SELECT DISTINCT ON (translator_id) translator_id, name FROM voices ORDER BY translator_id, name"))).all()) \
            if u.default_voice_filter else {}
    quiet_on = u.quiet_from is not None and u.quiet_to is not None
    quiet = f"{u.quiet_from:02d}:00–{u.quiet_to:02d}:00" if quiet_on else "выкл"
    delivery = f"дайджест раз в день в {u.digest_hour:02d}:00" if u.digest_hour is not None else "сразу"
    voice = ", ".join(names.get(t, f"#{t}") for t in u.default_voice_filter) if u.default_voice_filter else "любая"
    text_ = ("⚙️ <b>Настройки</b>\n\n"
             f"🖼 Картинки в уведомлениях: <b>{'вкл' if u.photos else 'выкл'}</b>\n"
             f"🌙 Тихие часы: <b>{quiet}</b>" + (" — ночью не пишу, отправлю утром" if quiet_on else "") + "\n"
             f"🕒 Часовой пояс: <b>{_tz_label(u.tz_offset)}</b>\n"
             f"📨 Доставка: <b>{delivery}</b>\n"
             f"🎙 Озвучка по умолчанию: <b>{voice}</b> — для новых подписок")
    rows = [[(f"🖼 Картинки: {'выключить' if u.photos else 'включить'}", "set:photos")],
            [(f"🌙 Тихие часы: {'выключить' if quiet_on else 'включить 23:00–08:00'}", "set:quiet")],
            [("🕒 −1 ч", "set:tz:-1"), (_tz_label(u.tz_offset), "noop"), ("🕒 +1 ч", "set:tz:1")],
            [(f"📨 {'Присылать сразу' if u.digest_hour is not None else f'Дайджест раз в день в {DIGEST_DEFAULT_HOUR}:00'}", "set:digest")],
            [("🎙 Озвучка по умолчанию", "set:voice")]]
    return text_, _kb(rows)


async def _render_default_voice(user_id: int):
    async with session() as s:
        u = await s.get(User, user_id)
        top = (await s.execute(text("SELECT translator_id, min(name), count(*) AS c FROM voices "
                                    "GROUP BY translator_id ORDER BY c DESC LIMIT 15"))).all()
    chosen = set(u.default_voice_filter or [])
    rows = [[("☑ Любая" if not chosen else "☐ Любая", "setv:any")]]
    rows += [[(f"{'☑' if tid in chosen else '☐'} {name[:30]}", f"setv:{tid}")] for tid, name, _ in top]
    rows.append([("« Готово", "set:back")])
    return ("🎙 <b>Озвучка по умолчанию</b>\nПрименяется к новым подпискам; у существующих меняется кнопкой 🎙 "
            "в «Мои подписки». Ниже — самые частые озвучки на сайте:"), _kb(rows)


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    parts = cb.data.split(":")
    what = parts[1]
    async with session() as s:
        await svc.upsert_user(s, cb.from_user.id, cb.from_user.username)
        u = await s.get(User, cb.from_user.id)
        if what == "photos":
            u.photos = not u.photos
        elif what == "quiet":
            u.quiet_from, u.quiet_to = (None, None) if u.quiet_from is not None else QUIET_DEFAULT
        elif what == "tz":
            u.tz_offset = max(-12, min(14, u.tz_offset + int(parts[2])))
        elif what == "digest":
            u.digest_hour = None if u.digest_hour is not None else DIGEST_DEFAULT_HOUR
        await s.commit()
    text_, kb = await (_render_default_voice if what == "voice" else _render_settings)(cb.from_user.id)
    await cb.answer()
    await _edit(cb, text_, kb)


@dp.callback_query(F.data.startswith("setv:"))
async def cb_default_voice(cb: CallbackQuery) -> None:
    if not guard.cheap_actions.allow(cb.from_user.id):
        await cb.answer(TOO_FAST)
        return
    val = cb.data.split(":")[1]
    async with session() as s:
        await svc.upsert_user(s, cb.from_user.id, cb.from_user.username)
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
    log.exception("Ошибка обработчика: %s", event.exception)
    upd = event.update
    try:
        if upd.message:
            await upd.message.answer("Что-то пошло не так. Попробуйте ещё раз чуть позже.")
        elif upd.callback_query:
            await upd.callback_query.answer("Что-то пошло не так. Попробуйте ещё раз.", show_alert=False)
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
    await bot.set_my_commands([
        BotCommand(command="my", description="Мои подписки"),
        BotCommand(command="new", description="Что вышло за неделю"),
        BotCommand(command="calendar", description="Календарь выхода серий"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    log.info("Бот запущен")
    try:
        # Накопившийся за простой бэклог не разгребаем; принимаем только нужные типы апдейтов.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
