"""Доменные операции над БД, общие для поллера и бота.

Здесь нет знания о Telegram и нет прямых HTTP-запросов, кроме sync_page(),
который читает страницу через RezkaClient и раскладывает её по таблицам.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Episode, EpisodeVoice, Franchise, Meta, Notification, Page, Schedule,
    Subscription, User, Voice, VoiceCheck,
)
from app.rezka.client import RezkaClient
from app.rezka.parser import FeedItem, ScheduleRow, TitlePage, franchise_name, parse_title_page

log = logging.getLogger(__name__)

# Интервалы отложенных проверок озвучек: 1ч, 3ч, 12ч, затем раз в сутки — до месяца.
VOICE_CHECK_DELAYS = [1, 3, 12] + [24] * 27
UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------------- users

async def upsert_user(s: AsyncSession, user_id: int, username: str | None) -> None:
    await s.execute(
        pg_insert(User)
        .values(id=user_id, username=username, is_active=True)
        .on_conflict_do_update(index_elements=[User.id],
                               set_={"username": username, "is_active": True, "blocked_at": None})
    )


# ----------------------------------------------------------------------------- pages

async def page_by_hid(s: AsyncSession, hdrezka_id: int) -> Page | None:
    return await s.scalar(select(Page).where(Page.hdrezka_id == hdrezka_id))


def year_from_meta(meta_line: str | None) -> str | None:
    """«2026, Япония, Фэнтези» → «2026»; «2024 - 2025, США» → «2024»."""
    m = re.match(r"\s*((?:19|20)\d{2})", meta_line or "")
    return m.group(1) if m else None


async def pages_to_read(s: AsyncSession, limit: int) -> list[Page]:
    """Очередь чтения: страницы, которых ещё не читали, — сначала с подписчиками, затем новые.
    Сюда попадают карточки ленты и поиска (каталог) и части франшиз из блока на странице."""
    rows = await s.execute(text("""
        SELECT p.id FROM pages p
         WHERE p.page_refreshed_at IS NULL AND p.url <> ''
         ORDER BY EXISTS (SELECT 1 FROM subscriptions sub
                           WHERE sub.page_id = p.id OR sub.franchise_id = p.franchise_id) DESC,
                  p.created_at DESC
         LIMIT :lim"""), {"lim": limit})
    return [await s.get(Page, pid) for (pid,) in rows]


async def upsert_page_from_feed(s: AsyncSession, item: FeedItem) -> Page:
    """Карточка ленты/поиска знает мало, но это дёшево и всегда свежо."""
    values = dict(hdrezka_id=item.hdrezka_id, title=item.title, url=item.url,
                  section=item.section, is_finished=item.is_finished,
                  meta_line=item.meta_line, year=year_from_meta(item.meta_line))
    if item.has_episode:
        values.update(last_season=item.season, last_episode=item.episode)
    if item.looks_like_film:
        values["content_type"] = None  # окончательно скажет только страница
    stmt = pg_insert(Page).values(**values)
    set_ = {k: v for k, v in values.items() if k != "hdrezka_id"}
    # Карточка без слова «Завершен» — не доказательство, что сезон идёт (сайт помечает не все).
    # Флаг снимаем только когда в карточке серия новее известной нам: сериал действительно вернулся.
    existing = Page.__table__.c
    # Карточка без строки «год, страна, жанр» не должна стирать известное.
    set_["meta_line"] = func.coalesce(stmt.excluded.meta_line, existing.meta_line)
    set_["year"] = func.coalesce(existing.year, stmt.excluded.year)
    set_["is_finished"] = case(
        (stmt.excluded.is_finished, True),
        (tuple_(stmt.excluded.last_season, stmt.excluded.last_episode)
         > tuple_(existing.last_season, existing.last_episode), False),
        else_=existing.is_finished,
    )
    stmt = (stmt.on_conflict_do_update(index_elements=[Page.hdrezka_id], set_=set_)
            .returning(Page.id))
    page_id = (await s.execute(stmt)).scalar_one()
    # populate_existing: объект мог быть загружен раньше в этой сессии — иначе вернём устаревшие поля.
    return await s.get(Page, page_id, populate_existing=True)


@dataclass
class SyncResult:
    page: Page
    franchise: Franchise | None
    franchise_was_known: bool        # франшиза уже была в БД до этого чтения
    new_parts: list[Page]            # части, которых в БД не было


async def sync_page(s: AsyncSession, client: RezkaClient, hdrezka_id: int, url: str) -> SyncResult:
    """Читает страницу тайтла и обновляет pages / voices / schedule / franchises.
    Один HTTP-запрос. Новые части франшизы возвращает, но НЕ уведомляет — это
    решает вызывающий: при первом знакомстве с франшизой они не «новые» для сайта."""
    html = await client.title_page(url)
    tp = parse_title_page(html, url)
    if tp.hdrezka_id and tp.hdrezka_id != hdrezka_id:
        log.warning("Страница %s отдала id %s (редирект на другое зеркало/слаг?)", hdrezka_id, tp.hdrezka_id)

    page = await page_by_hid(s, hdrezka_id)
    if page is None:
        page = Page(hdrezka_id=hdrezka_id, title=tp.title or url, url=url)
        s.add(page)
        await s.flush()

    if tp.title:
        page.title = tp.title
    page.orig_title = tp.orig_title or page.orig_title
    page.url = url
    page.content_type = tp.content_type or page.content_type
    page.default_translator = tp.default_translator
    if tp.poster_url and tp.poster_url != page.poster_url:
        page.poster_url, page.poster_file_id = tp.poster_url, None   # другая картинка — старый file_id не годится
    # Позиция плеера (initCDNSeriesEvents) у завершённых стоит на 1×1 — не годится.
    # Надёжнее максимум по списку серий, встроенному в страницу.
    if tp.episodes:
        last_s = max(tp.episodes)
        page.last_season, page.last_episode = last_s, max(tp.episodes[last_s])
    elif tp.current_season is not None:
        page.last_season, page.last_episode = tp.current_season, tp.current_episode
    page.page_refreshed_at = now()

    # Озвучки — заменяем целиком: список на сайте авторитетен.
    await s.execute(delete(Voice).where(Voice.page_id == page.id))
    for t in tp.translators:
        s.add(Voice(page_id=page.id, translator_id=t.id, name=t.name))

    for row in tp.schedule:
        await s.execute(
            pg_insert(Schedule)
            .values(page_id=page.id, season=row.season, episode=row.episode,
                    title=row.title, air_date=row.air_date, aired=row.aired)
            .on_conflict_do_update(index_elements=[Schedule.page_id, Schedule.season, Schedule.episode],
                                   set_={"title": row.title, "air_date": row.air_date, "aired": row.aired})
        )

    if tp.content_type == "series" and (schedule_finished(tp.schedule)
                                        or finished_by_silence(page, bool(tp.schedule))):
        page.is_finished = True

    franchise, was_known, new_parts = await _apply_franchise(s, page, tp)
    await s.flush()
    return SyncResult(page, franchise, was_known, new_parts)


FINISHED_AFTER_DAYS = 60


def schedule_finished(rows: list[ScheduleRow]) -> bool:
    """Сайт помечает «Завершен» не все сезоны: у «Поднятия уровня в одиночку 2» все 13 серий вышли
    в марте 2025, а карточка по-прежнему «2 сезон, 13 серия». Считаем по расписанию: все серии вышли,
    будущих нет, последняя — давно (60 дней: перерыв между кура/полусезонами без объявленных дат короче).
    Ошибка в сторону «завершён» безопасна — новая серия в ленте вернёт статус (upsert_page_from_feed)."""
    if not rows or not all(r.aired for r in rows):
        return False
    last = max((r.air_date for r in rows if r.air_date), default=None)
    return last is not None and (date.today() - last).days > FINISHED_AFTER_DAYS


QUIET_DAYS = 30


def _known_since(page: Page) -> datetime | None:
    """created_at без похода в БД: у страницы, созданной в этой же сессии, server_default ещё не
    загружен, а ленивая загрузка в async-сессии — ошибка. Не загружено = знаем только что."""
    return page.__dict__.get("created_at")


def finished_by_silence(page: Page, has_schedule: bool, at: datetime | None = None) -> bool:
    """Сериал без расписания на сайте (части франшиз, старые тайтлы): дат нет, «Завершен» в карточке
    сайт ставит не всем — такие страницы считались идущими и перечитывались каждую неделю.
    Завершён, если год старше текущего и серий в ленте не было QUIET_DAYS: часть двухлетней давности
    и старше — сразу при чтении, прошлогодняя — когда страница известна боту QUIET_DAYS без серий
    (перерыв между курами короче). Текущий год — никогда: решают расписание и лента. Есть расписание —
    решает только оно (schedule_finished). Ошибка в сторону «завершён» безопасна: новая серия в ленте
    снимает флаг (upsert_page_from_feed)."""
    if has_schedule:
        return False
    at = at or now()
    y = _year(page)
    if y is None or y >= at.year:
        return False
    if page.last_event_at is not None and (at - page.last_event_at).days < QUIET_DAYS:
        return False
    if y <= at.year - 2:
        return True
    known = _known_since(page)
    return known is not None and (at - known).days >= QUIET_DAYS


async def _apply_franchise(s: AsyncSession, page: Page, tp: TitlePage):
    if not tp.franchise:
        return None, False, []

    ids = set(tp.franchise_ids) | {page.hdrezka_id}
    key = min(ids)

    fr = await s.scalar(select(Franchise).where(Franchise.key_hdrezka_id == key))
    was_known = fr is not None
    if fr is None:
        # Кто-то из участников мог быть привязан к франшизе с другим ключом
        # (например, читали блок, когда самой ранней части ещё не было) — переиспользуем.
        fr = await s.scalar(
            select(Franchise).join(Page, Page.franchise_id == Franchise.id)
            .where(Page.hdrezka_id.in_(ids)).limit(1)
        )
        was_known = fr is not None
        if fr is not None:
            fr.key_hdrezka_id = key
        else:
            fr = Franchise(key_hdrezka_id=key, name="")
            s.add(fr)
            await s.flush()

    titles = [(p.title, p.year) for p in tp.franchise if p.title]
    fr.name = franchise_name(titles) or fr.name or tp.title
    fr.refreshed_at = now()

    new_parts: list[Page] = []
    for part in tp.franchise:
        if part.is_current or not part.hdrezka_id:
            continue
        existing = await page_by_hid(s, part.hdrezka_id)
        if existing is None:
            existing = Page(hdrezka_id=part.hdrezka_id, title=part.title, url=part.url or "",
                            year=part.year, franchise_id=fr.id)
            s.add(existing)
            new_parts.append(existing)
        else:
            existing.franchise_id = fr.id
            if part.year and not existing.year:
                existing.year = part.year
    page.franchise_id = fr.id
    await s.flush()
    return fr, was_known, new_parts


async def franchise_anchor(s: AsyncSession, franchise_id: int) -> Page | None:
    """Любая часть годится (блок одинаковый) — но предпочитаем выходящий сериал:
    заодно обновим его расписание."""
    return await s.scalar(
        select(Page).where(Page.franchise_id == franchise_id, Page.url != "")
        .order_by(Page.is_finished.asc(), (Page.content_type == "series").desc(), Page.id.desc())
        .limit(1)
    )


# ----------------------------------------------------------------------------- episodes & fan-out

async def record_episode(s: AsyncSession, page: Page, season: int, episode: int) -> int | None:
    """Возвращает id серии, только если она новая. UNIQUE делает операцию идемпотентной."""
    stmt = (pg_insert(Episode).values(page_id=page.id, season=season, episode=episode)
            .on_conflict_do_nothing(constraint="uq_episode").returning(Episode.id))
    return (await s.execute(stmt)).scalar_one_or_none()


_SUB_MATCH = "(sub.page_id = :page_id OR sub.franchise_id = :franchise_id)"


async def enqueue_episode(s: AsyncSession, page: Page, episode_id: int) -> int:
    """Подписчикам без фильтра озвучек — сразу. Одним INSERT … SELECT, без выгрузки в память."""
    r = await s.execute(text(f"""
        INSERT INTO notifications (user_id, kind, ref_id, next_attempt_at)
        SELECT DISTINCT sub.user_id, 'episode', CAST(:episode_id AS bigint),
               notify_at(u.tz_offset, u.quiet_from, u.quiet_to, u.digest_hour)
          FROM subscriptions sub JOIN users u ON u.id = sub.user_id AND u.is_active
         WHERE sub.voice_filter IS NULL AND {_SUB_MATCH}
        ON CONFLICT ON CONSTRAINT uq_notification DO NOTHING
    """), {"episode_id": episode_id, "page_id": page.id, "franchise_id": page.franchise_id or -1})
    return r.rowcount or 0


async def wanted_translators(s: AsyncSession, page: Page) -> set[int]:
    """Озвучки, которые ждут подписчики этой страницы/франшизы с фильтром."""
    rows = await s.execute(text(f"""
        SELECT DISTINCT unnest(sub.voice_filter)
          FROM subscriptions sub JOIN users u ON u.id = sub.user_id AND u.is_active
         WHERE sub.voice_filter IS NOT NULL AND {_SUB_MATCH}
    """), {"page_id": page.id, "franchise_id": page.franchise_id or -1})
    return {r[0] for r in rows}


async def enqueue_voice(s: AsyncSession, page: Page, episode_id: int, translator_id: int) -> int:
    r = await s.execute(text(f"""
        INSERT INTO notifications (user_id, kind, ref_id, next_attempt_at)
        SELECT DISTINCT sub.user_id, CAST(:kind AS varchar), CAST(:episode_id AS bigint),
               notify_at(u.tz_offset, u.quiet_from, u.quiet_to, u.digest_hour)
          FROM subscriptions sub JOIN users u ON u.id = sub.user_id AND u.is_active
         WHERE sub.voice_filter @> ARRAY[CAST(:tid AS int)] AND {_SUB_MATCH}
        ON CONFLICT ON CONSTRAINT uq_notification DO NOTHING
    """), {"kind": f"voice:{translator_id}", "episode_id": episode_id, "tid": translator_id,
           "page_id": page.id, "franchise_id": page.franchise_id or -1})
    return r.rowcount or 0


async def enqueue_new_part(s: AsyncSession, franchise_id: int, part: Page, user_id: int | None = None) -> int:
    """Всем подписчикам франшизы; с user_id — только ему (ждавший продолжения переведён на франшизу,
    остальные подписчики об этой части уже знают)."""
    r = await s.execute(text("""
        INSERT INTO notifications (user_id, kind, ref_id, next_attempt_at)
        SELECT DISTINCT sub.user_id, 'new_part', CAST(:part_id AS bigint),
               notify_at(u.tz_offset, u.quiet_from, u.quiet_to, u.digest_hour)
          FROM subscriptions sub JOIN users u ON u.id = sub.user_id AND u.is_active
         WHERE sub.franchise_id = :franchise_id AND (CAST(:uid AS bigint) IS NULL OR sub.user_id = :uid)
        ON CONFLICT ON CONSTRAINT uq_notification DO NOTHING
    """), {"part_id": part.id, "franchise_id": franchise_id, "uid": user_id})
    return r.rowcount or 0


# ----------------------------------------------------------------------------- «жду продолжения»

async def waiting_subscriptions(s: AsyncSession, franchise_id: int) -> list[tuple[int, int]]:
    """Подписки на завершённые части франшизы = «жду продолжения» (§7, решение 3): (user_id, page_id)."""
    rows = await s.execute(text("""
        SELECT sub.user_id, sub.page_id FROM subscriptions sub
          JOIN pages p ON p.id = sub.page_id
         WHERE p.franchise_id = :f AND p.is_finished
         ORDER BY sub.id"""), {"f": franchise_id})
    return [(u, pid) for u, pid in rows]


def _year(p: Page) -> int | None:
    try:
        return int((p.year or "")[:4])
    except ValueError:
        return None


def continuation_parts(parts: list[Page], waited: Page) -> list[Page]:
    """Части франшизы, о которых стоит сообщить ждавшему продолжения: не та, которую ждали,
    не завершённые, не старше ждавшейся по году (старые части и старые фильмы — не продолжение).
    Непрочитанные (content_type NULL) проходят: вызывающий дочитает их и отфильтрует ещё раз."""
    wy = _year(waited)
    out = []
    for p in parts:
        if p.id == waited.id or p.is_finished:
            continue
        py = _year(p)
        if wy is not None and py is not None and py < wy:
            continue
        out.append(p)
    return out


async def mark_voice_seen(s: AsyncSession, episode_id: int, translator_id: int) -> bool:
    stmt = (pg_insert(EpisodeVoice).values(episode_id=episode_id, translator_id=translator_id)
            .on_conflict_do_nothing().returning(EpisodeVoice.episode_id))
    return (await s.execute(stmt)).scalar_one_or_none() is not None


async def schedule_voice_check(s: AsyncSession, episode_id: int, translator_id: int, attempts: int) -> None:
    if attempts >= len(VOICE_CHECK_DELAYS):
        await s.execute(delete(VoiceCheck).where(VoiceCheck.episode_id == episode_id,
                                                 VoiceCheck.translator_id == translator_id))
        return
    nxt = now() + timedelta(hours=VOICE_CHECK_DELAYS[attempts])
    await s.execute(
        pg_insert(VoiceCheck).values(episode_id=episode_id, translator_id=translator_id,
                                     next_check_at=nxt, attempts=attempts)
        .on_conflict_do_update(index_elements=[VoiceCheck.episode_id, VoiceCheck.translator_id],
                               set_={"next_check_at": nxt, "attempts": attempts})
    )


# ----------------------------------------------------------------------------- локальный поиск

WORD_SIMILARITY_THRESHOLD = 0.6   # «слиз» → «…в слизь» = 0.8; при 0.5 «дом дракона» тянул «Кот и дракон»

SEARCH_SQL = text("""
    SELECT p.id,
           greatest(word_similarity(norm_title(CAST(:q AS text)), norm_title(p.title)),
                    word_similarity(norm_title(CAST(:q AS text)), norm_title(p.orig_title))) AS score
      FROM pages p
     WHERE norm_title(CAST(:q AS text)) <% norm_title(p.title) OR norm_title(CAST(:q AS text)) <% norm_title(p.orig_title)
     ORDER BY score DESC, (NOT p.is_finished) DESC, p.last_event_at DESC NULLS LAST, p.id DESC
     LIMIT :lim""")


async def search_catalog(s: AsyncSession, query: str, limit: int = 30) -> list[Page]:
    """Поиск по каталогу: триграммы (pg_trgm) по названию и оригинальному названию, без ё/диакритики
    (unaccent). Ответ — миллисекунды и ноль запросов к сайту."""
    await s.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}"))
    rows = (await s.execute(SEARCH_SQL, {"q": query, "lim": limit})).all()
    return [await s.get(Page, pid) for pid, _ in rows]


async def franchise_stats(s: AsyncSession, ids: list[int]) -> dict[int, tuple[str, int, int]]:
    """franchise_id → (имя, всего частей, из них выходят)."""
    rows = await s.execute(text("""
        SELECT f.id, f.name, count(p.id),
               count(*) FILTER (WHERE NOT p.is_finished AND coalesce(p.content_type, 'series') <> 'film'
                                  AND p.last_episode IS NOT NULL)
          FROM franchises f JOIN pages p ON p.franchise_id = f.id
         WHERE f.id = ANY(CAST(:ids AS int[])) GROUP BY f.id, f.name"""), {"ids": ids})
    return {fid: (name, parts, ongoing) for fid, name, parts, ongoing in rows}


# ----------------------------------------------------------------------------- subscriptions

async def _default_voices(s: AsyncSession, user_id: int) -> list[int] | None:
    return await s.scalar(text("SELECT default_voice_filter FROM users WHERE id = :u"), {"u": user_id})


async def subscribe_page(s: AsyncSession, user_id: int, page_id: int) -> bool:
    stmt = (pg_insert(Subscription).values(user_id=user_id, scope="page", page_id=page_id,
                                           voice_filter=await _default_voices(s, user_id))
            .on_conflict_do_nothing(index_elements=["user_id", "page_id"],
                                    index_where=text("page_id IS NOT NULL"))
            .returning(Subscription.id))
    return (await s.execute(stmt)).scalar_one_or_none() is not None


async def subscribe_franchise(s: AsyncSession, user_id: int, franchise_id: int) -> bool:
    """Подписка на франшизу поглощает подписки на её страницы — в /my одна строка."""
    stmt = (pg_insert(Subscription).values(user_id=user_id, scope="franchise", franchise_id=franchise_id,
                                           voice_filter=await _default_voices(s, user_id))
            .on_conflict_do_nothing(index_elements=["user_id", "franchise_id"],
                                    index_where=text("franchise_id IS NOT NULL"))
            .returning(Subscription.id))
    created = (await s.execute(stmt)).scalar_one_or_none() is not None
    await s.execute(text("""
        DELETE FROM subscriptions sub USING pages p
         WHERE sub.page_id = p.id AND sub.user_id = :uid AND p.franchise_id = :fid
    """), {"uid": user_id, "fid": franchise_id})
    return created


async def unsubscribe(s: AsyncSession, user_id: int, sub_id: int) -> bool:
    r = await s.execute(delete(Subscription).where(Subscription.id == sub_id,
                                                   Subscription.user_id == user_id))
    return (r.rowcount or 0) > 0


async def set_voice_filter(s: AsyncSession, user_id: int, sub_id: int, translators: list[int] | None) -> None:
    sub = await s.get(Subscription, sub_id)
    if sub and sub.user_id == user_id:
        sub.voice_filter = sorted(set(translators)) if translators else None


# ----------------------------------------------------------------------------- meta

async def meta_get(s: AsyncSession, key: str) -> str | None:
    return await s.scalar(select(Meta.value).where(Meta.key == key))


async def meta_set(s: AsyncSession, key: str, value: str) -> None:
    await s.execute(pg_insert(Meta).values(key=key, value=value)
                    .on_conflict_do_update(index_elements=[Meta.key], set_={"value": value, "updated_at": func.now()}))
