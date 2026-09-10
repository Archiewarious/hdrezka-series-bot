"""Разбор HTML. Все селекторы проверены на живых страницах 04–05.09.2026
(docs/ARCHITECTURE.md, §1.1). Никаких запросов к сети здесь нет.

Карточка ленты/поиска:
    div.b-content__inline_item[data-id][data-url]
        div.b-content__inline_item-cover
            span.cat.<section>       — раздел: series / animation / cartoons / films
            span.info                — «4 сезон, 21 серия» | «Завершен (все серии)» | пусто (фильм)
        div.b-content__inline_item-link
            a                        — название
            div                      — «2026, Япония, Фэнтези» (год, страна, жанр)
    Обложка 166×250 в карточке есть, но для уведомлений непригодна — не берём.

Страница тайтла:
    .b-post__title h1                          — название
    .b-post__origtitle                         — оригинальное название
    .b-sidecover a[href]                       — постер полного размера (1061×1500, ~1 МБ); запас — meta[property=og:image] (250×360)
    initCDNSeriesEvents(id, translator, s, e)  — сериал; initCDNMoviesEvents(id, …) — фильм
    .b-translator__item[data-translator_id]    — озвучки (дублируются в DOM; нет, если одна)
    .b-simple_episode__item[data-season_id][data-episode_id] — серии озвучки по умолчанию
    .b-post__partcontent_item[data-url]        — части франшизы; текущая: class current, без data-url
    .b-post__schedule_list tr                  — расписание: td[0] «4 сезон 21 серия», td[3] дата, td[4] статус
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from selectolax.parser import HTMLParser, Node

_EPISODE_RX = re.compile(r"(?:(\d+)\s*сезон)?[,\s]*(\d+)\s*сери[яйи]", re.I)
_FINISHED_RX = re.compile(r"завершен", re.I)
_ID_IN_URL_RX = re.compile(r"/(\d+)-[^/]*\.html")
_CDN_SERIES_RX = re.compile(r"initCDNSeriesEvents\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)")
_CDN_MOVIE_RX = re.compile(r"initCDNMoviesEvents\((\d+),\s*(\d+)")
_SCHED_NUM_RX = re.compile(r"(\d+)\s*сезон\s*(\d+)\s*серия", re.I)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_DATE_RX = re.compile(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", re.I)


def _text(node: Node | None) -> str | None:
    return node.text(strip=True) if node else None


# ----------------------------------------------------------------------------- лента / поиск

@dataclass(frozen=True)
class FeedItem:
    hdrezka_id: int
    title: str
    url: str
    section: str | None
    info: str
    season: int | None
    episode: int | None
    is_finished: bool
    meta_line: str | None = None       # «2026, Япония, Фэнтези»

    @property
    def has_episode(self) -> bool:
        return self.episode is not None

    @property
    def looks_like_film(self) -> bool:
        """У фильмов подпись пустая. Окончательно тип известен только со страницы."""
        return not self.info


def parse_feed(html: str) -> list[FeedItem]:
    """Лента, каталог и поиск — верстка карточек одна."""
    items: list[FeedItem] = []
    for node in HTMLParser(html).css("div.b-content__inline_item"):
        raw_id = node.attributes.get("data-id")
        url = node.attributes.get("data-url")
        link = node.css_first("div.b-content__inline_item-link > a")
        if not (raw_id and raw_id.isdigit() and url and link):
            continue

        info_node = node.css_first("div.b-content__inline_item-cover span.info")
        info = info_node.text(strip=True) if info_node else ""

        section = None
        if cat := node.css_first("div.b-content__inline_item-cover span.cat"):
            classes = (cat.attributes.get("class") or "").split()
            section = next((c for c in classes if c != "cat"), None)

        season = episode = None
        finished = bool(_FINISHED_RX.search(info))
        if info and not finished and (m := _EPISODE_RX.search(info)):
            season = int(m.group(1)) if m.group(1) else 1
            episode = int(m.group(2))

        meta = node.css_first("div.b-content__inline_item-link > div")
        items.append(FeedItem(int(raw_id), link.text(strip=True), url, section, info,
                              season, episode, finished, meta_line=_text(meta) or None))
    return items


# ----------------------------------------------------------------------------- страница тайтла

@dataclass(frozen=True)
class Translator:
    id: int
    name: str


@dataclass(frozen=True)
class FranchisePart:
    hdrezka_id: int | None      # None у текущей части — это id самой страницы
    title: str
    year: str | None
    url: str | None
    is_current: bool


@dataclass(frozen=True)
class ScheduleRow:
    season: int
    episode: int
    title: str | None
    air_date: date | None       # None, если на сайте указан только год
    status: str                 # '✓' | 'сегодня' | 'через N дней' | ''
    aired: bool


@dataclass
class TitlePage:
    hdrezka_id: int | None
    title: str
    orig_title: str | None
    content_type: str | None            # 'series' | 'film' | None
    default_translator: int | None
    current_season: int | None
    current_episode: int | None
    translators: list[Translator] = field(default_factory=list)
    episodes: dict[int, list[int]] = field(default_factory=dict)   # сезон → серии (озвучка по умолчанию)
    franchise: list[FranchisePart] = field(default_factory=list)
    schedule: list[ScheduleRow] = field(default_factory=list)
    poster_url: str | None = None

    @property
    def franchise_ids(self) -> set[int]:
        ids = {p.hdrezka_id for p in self.franchise if p.hdrezka_id}
        if self.franchise and self.hdrezka_id:
            ids.add(self.hdrezka_id)
        return ids


def parse_ru_date(s: str) -> date | None:
    m = _DATE_RX.search(s or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def parse_title_page(html: str, url: str | None = None) -> TitlePage:
    tree = HTMLParser(html)

    hdrezka_id = default_translator = cur_season = cur_episode = None
    content_type = None
    if m := _CDN_SERIES_RX.search(html):
        hdrezka_id, default_translator, cur_season, cur_episode = map(int, m.groups())
        content_type = "series"
    elif m := _CDN_MOVIE_RX.search(html):
        hdrezka_id, default_translator = int(m.group(1)), int(m.group(2))
        content_type = "film"
    elif url and (m := _ID_IN_URL_RX.search(url)):
        hdrezka_id = int(m.group(1))

    title = _text(tree.css_first(".b-post__title h1")) or _text(tree.css_first("h1")) or ""
    # Постер: полноразмерный по ссылке с превью; og:image (250×360) — только если ссылки нет.
    full = tree.css_first(".b-sidecover a")
    og = tree.css_first('meta[property="og:image"]')
    poster_url = ((full.attributes.get("href") if full else None)
                  or (og.attributes.get("content") if og else None) or None)

    # Озвучки: список дублируется (desktop + mobile) — берём уникальные по id, порядок сохраняем.
    translators: list[Translator] = []
    seen: set[int] = set()
    for n in tree.css(".b-translator__item"):
        raw = n.attributes.get("data-translator_id")
        if raw and raw.isdigit() and int(raw) not in seen:
            seen.add(int(raw))
            translators.append(Translator(int(raw), n.text(strip=True) or f"#{raw}"))
    if not translators and default_translator is not None:
        # Одна озвучка — списка нет; имя в строке «В переводе» инфо-таблицы.
        name = None
        for row in tree.css(".b-post__info tr"):
            h = row.css_first("h2")
            if h and "перевод" in h.text().lower():
                name = row.text(strip=True).replace(h.text(strip=True), "").lstrip(": ").strip()
        translators.append(Translator(default_translator, name or "Основная"))

    episodes: dict[int, list[int]] = {}
    for n in tree.css(".b-simple_episode__item"):
        s, e = n.attributes.get("data-season_id"), n.attributes.get("data-episode_id")
        if s and e and s.isdigit() and e.isdigit():
            episodes.setdefault(int(s), []).append(int(e))

    franchise: list[FranchisePart] = []
    for n in tree.css(".b-post__partcontent_item"):
        part_url = n.attributes.get("data-url")
        m = _ID_IN_URL_RX.search(part_url or "")
        year = _text(n.css_first(".year"))
        franchise.append(FranchisePart(
            hdrezka_id=int(m.group(1)) if m else None,
            title=_text(n.css_first(".title")) or "",
            year=year.replace("год", "").strip() if year else None,
            url=part_url,
            is_current="current" in (n.attributes.get("class") or ""),
        ))

    schedule: list[ScheduleRow] = []
    for row in tree.css(".b-post__schedule_list tr"):
        tds = row.css("td")
        if len(tds) < 5:
            continue
        m = _SCHED_NUM_RX.search(tds[0].text(strip=True))
        if not m:
            continue
        status = tds[4].text(strip=True)
        schedule.append(ScheduleRow(
            season=int(m.group(1)), episode=int(m.group(2)),
            title=_text(tds[1].css_first("b")) or _text(tds[1]) or None,
            air_date=parse_ru_date(tds[3].text(strip=True)),
            status=status,
            aired=status in ("✓", "сегодня"),
        ))

    return TitlePage(hdrezka_id, title, _text(tree.css_first(".b-post__origtitle")), content_type,
                     default_translator, cur_season, cur_episode, translators, episodes,
                     franchise, schedule, poster_url)


def parse_episodes_html(html: str) -> set[tuple[int, int]]:
    """Поле `episodes` из ответа /ajax/get_cdn_series/ → множество (сезон, серия)."""
    out: set[tuple[int, int]] = set()
    for n in HTMLParser(html or "").css(".b-simple_episode__item"):
        s, e = n.attributes.get("data-season_id"), n.attributes.get("data-episode_id")
        if s and e and s.isdigit() and e.isdigit():
            out.add((int(s), int(e)))
    return out


def franchise_name(titles: list[tuple[str, str | None]]) -> str:
    """Имя франшизы для показа: общий префикс названий (не короче 8 символов, до
    границы слова), иначе — название самой ранней части без хвостов вида «[ТВ-1]»."""
    names = [t for t, _ in titles if t]
    if not names:
        return ""
    prefix = names[0]
    for n in names[1:]:
        i = 0
        while i < min(len(prefix), len(n)) and prefix[i].lower() == n[i].lower():
            i += 1
        prefix = prefix[:i]
    prefix = re.split(r"[\[:(/—-]", prefix)[0].rstrip(" .,")
    if len(prefix) >= 8:
        return prefix
    earliest = min(titles, key=lambda t: (t[1] or "9999"))[0]
    return re.split(r"\s*[\[:(/]", earliest)[0].strip() or earliest


# ----------------------------------------------------------------------------- блок «Обновления»

@dataclass(frozen=True)
class UpdateItem:
    """Событие из блока «Обновления» на главной: серия вышла в конкретной озвучке (F13)."""
    day: date | None            # из заголовка группы: «Сегодня (10 сентября 2026)» → 2026-09-10
    hdrezka_id: int
    title: str
    url: str                    # путь, как в href: /animation/fantasy/90694-krestyanin-999-urovnya-2026.html
    section: str | None         # первый сегмент пути: series / animation / cartoons
    season: int
    episode: int
    voice: str | None           # «Дубляж», «Субтитры», «FanVoxUA (Украинский)»; у части событий озвучки нет


def parse_updates(html: str) -> list[UpdateItem]:
    """Неделя событий по дням, свежие сверху. В отличие от лент разделов (F8) упорядочен по времени
    выхода и знает озвучку; одна серия встречается по разу на каждую озвучку."""
    tree = HTMLParser(html)
    out: list[UpdateItem] = []
    for block in tree.css(".b-seriesupdate__block"):
        head = block.css_first(".b-seriesupdate__block_date")
        day = parse_ru_date(head.text(separator=" ") if head else "")
        for li in block.css(".b-seriesupdate__block_list_item"):
            a = li.css_first(".b-seriesupdate__block_list_link")
            href = (a.attributes.get("href") or "") if a else ""
            m = _ID_IN_URL_RX.search(href)
            season_node, cell = li.css_first(".season"), li.css_first(".cell-2")
            sm = re.search(r"(\d+)", season_node.text() if season_node else "")
            em = re.search(r"(\d+)\s*сери", cell.text() if cell else "", re.I)
            if not (a and m and sm and em):
                continue
            voice_node = cell.css_first("i")
            # Снимаем одну внешнюю пару скобок: «(FanVoxUA (Украинский))» → «FanVoxUA (Украинский)».
            # strip("()") срезал бы и закрывающую скобку внутреннего уточнения.
            voice = voice_node.text(strip=True) if voice_node else ""
            if voice.startswith("(") and voice.endswith(")"):
                voice = voice[1:-1].strip()
            section = href.strip("/").split("/", 1)[0] or None
            out.append(UpdateItem(day, int(m.group(1)), a.text(strip=True), href, section,
                                  int(sm.group(1)), int(em.group(1)), voice or None))
    return out


_VOICE_SYNONYMS = {"субтитры": "оригинал"}


def norm_voice(name: str | None) -> str:
    """Имя озвучки для сопоставления блока обновлений со списком на странице тайтла:
    «FanVoxUA (Украинский)» ↔ «FanVoxUA», «Субтитры» ↔ «Оригинал (+субтитры)»."""
    key = re.sub(r"\(.*?(?:\)|$)", " ", (name or "").lower())      # и незакрытая скобка тоже
    key = re.sub(r"[^0-9a-zа-яё]+", "", key)
    return _VOICE_SYNONYMS.get(key, key)


PREFIX_MATCH_MIN = 5


def match_voice(index: dict[str, int], voice: str | None) -> int | None:
    """translator_id для озвучки из блока обновлений по списку страницы (ключи — norm_voice).
    Сначала точное совпадение, иначе единственное совпадение по началу имени: «многоголосый» в блоке ↔
    «Многоголосый закадровый» на странице. Несколько кандидатов или короткое имя — не угадываем."""
    key = norm_voice(voice)
    if not key:
        return None
    if key in index:
        return index[key]
    cands = {tid for name, tid in index.items()
             if min(len(name), len(key)) >= PREFIX_MATCH_MIN and (name.startswith(key) or key.startswith(name))}
    return cands.pop() if len(cands) == 1 else None

