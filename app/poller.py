"""Поллер: единственный процесс, который ходит на сайт по расписанию.

Потоки (docs/ARCHITECTURE.md, §4.1):
  1. блок «Обновления» на главной — единственный источник событий: новые серии и озвучки (F13);
  2. суточная сверка состава франшиз с подписчиками: фильмы и спин-оффы, которых в блоке нет;
  3. недельное перечитывание страниц с подписчиками: расписание, озвучки, статус;
  4. очередь чтения каталога: страницы, которых ещё не читали.

Число запросов к сайту не зависит от числа пользователей.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, text

from app import service as svc
from app.config import cfg
from app.db import engine, init_db, session
from app.models import Franchise, Page
from app.rezka.client import AccessBlocked, RezkaClient
from app.rezka.parser import UpdateItem, match_voice, parse_updates

log = logging.getLogger("poller")

ADVISORY_LOCK_KEY = 0x52455A4B          # "REZK" — ровно один поллер на базу
EVENT_READS = 10                        # чтений страниц ради событий за цикл; не хватило — событие ждёт цикла
VOICE_REREAD = timedelta(hours=24)      # незнакомая озвучка → перечитать список озвучек страницы, не чаще
FRANCHISE_REFRESH_HOURS = 24
PAGE_REFRESH_DAYS = 7
PER_CYCLE_FRANCHISES = 2                # чтобы обход не растягивался: остальное — в следующий цикл
PER_CYCLE_PAGES = 2
PER_CYCLE_READS = 8                     # очередь чтения каталога
READ_GIVE_UP_AFTER = 3                  # страница не читается N раз подряд → идём дальше без неё


class Deferred(Exception):
    """Событию нужно чтение страницы, а лимит цикла исчерпан. Разберём в следующем цикле: блок хранит
    событие неделю, а от этой попытки в базе ничего не осталось — точка сохранения откатилась."""


class Poller:
    def __init__(self, client: RezkaClient | None = None) -> None:
        self.client = client or RezkaClient()
        self._read_failures: dict[int, int] = {}
        self._cycle_new_parts = 0               # уведомлений new_part за цикл — для строки лога
        self._reads_left = 0

    # ------------------------------------------------------------------ чтение страниц

    async def sync(self, s, hdrezka_id: int, url: str) -> svc.SyncResult:
        """Читает страницу. Если франшиза уже была известна, о недавних частях, которых в базе не было,
        сообщает подписчикам: так приходят фильмы и спин-оффы — в блоке обновлений только серии."""
        res = await svc.sync_page(s, self.client, hdrezka_id, url)
        if res.franchise and res.franchise_was_known and res.new_parts:
            has_subs = await s.scalar(text("""
                SELECT 1 FROM subscriptions sub LEFT JOIN pages p ON p.id = sub.page_id
                 WHERE sub.franchise_id = :f OR p.franchise_id = :f LIMIT 1"""), {"f": res.franchise.id})
            for part in res.new_parts:
                if not (has_subs and part.url and svc.is_recent_part(part)):
                    continue
                try:
                    await svc.sync_page(s, self.client, part.hdrezka_id, part.url)   # тип части — для текста
                except AccessBlocked as exc:
                    log.warning("Не прочитал новую часть %s: %s", part.hdrezka_id, exc)
                n = await svc.enqueue_new_part(s, res.franchise.id, part)
                self._cycle_new_parts += n
                log.info("НОВАЯ ЧАСТЬ франшизы «%s»: %s → %s подписчикам", res.franchise.name, part.title, n)
        if res.franchise:
            await self._adopt_waiting(s, res.franchise)
        return res

    async def _adopt_waiting(self, s, fr: Franchise) -> None:
        """«Жду продолжения» (§7, решение 3): подписка на завершённую часть франшизы переводится
        на франшизу, а о частях-продолжениях (не завершены, не старше ждавшейся) уходит new_part.
        Идемпотентно: после перевода ждущих у франшизы не остаётся."""
        waiting = await svc.waiting_subscriptions(s, fr.id)
        if not waiting:
            return
        parts = (await s.execute(select(Page).where(Page.franchise_id == fr.id))).scalars().all()
        by_id = {p.id: p for p in parts}
        moved = queued = 0
        for user_id, page_id in waiting:
            waited = by_id.get(page_id)
            if waited is None:
                continue
            for part in svc.continuation_parts(parts, waited):
                if part.page_refreshed_at is None and part.url:      # тип и статус части — со страницы
                    try:
                        await svc.sync_page(s, self.client, part.hdrezka_id, part.url)
                    except AccessBlocked as exc:
                        log.warning("Не прочитал часть %s для ждущего продолжения: %s", part.hdrezka_id, exc)
            await svc.subscribe_franchise(s, user_id, fr.id)          # удаляет и подписку на страницу
            moved += 1
            for part in svc.continuation_parts(parts, waited):
                queued += await svc.enqueue_new_part(s, fr.id, part, user_id=user_id)
        self._cycle_new_parts += queued
        log.info("ПРОДОЛЖЕНИЕ: франшиза «%s» — переведено подписок %s, уведомлений %s", fr.name, moved, queued)

    async def _read(self, s, page: Page, required: bool) -> bool:
        """Чтение страницы ради события. Обязательное — новая серия на непрочитанной странице: без привязки
        к франшизе её подписчики уведомления не получат; при исчерпанном лимите событие откладывается.
        Необязательное — обновить список озвучек: берёт не больше половины лимита и просто пропускается."""
        if self._reads_left <= (0 if required else EVENT_READS // 2):
            if required:
                raise Deferred()
            return False
        self._reads_left -= 1
        try:
            await self.sync(s, page.hdrezka_id, page.url)
            self._read_failures.pop(page.hdrezka_id, None)
            return True
        except AccessBlocked as exc:
            n = self._read_failures[page.hdrezka_id] = self._read_failures.get(page.hdrezka_id, 0) + 1
            if required and n < READ_GIVE_UP_AFTER:
                log.warning("Страница %s не прочиталась ради события (%s-й раз), повторю в следующем цикле: %s",
                            page.hdrezka_id, n, exc)
                raise Deferred() from exc
            log.warning("Страница %s не прочиталась ради события, иду дальше без неё: %s", page.hdrezka_id, exc)
            return False

    # ------------------------------------------------------------------ 1: блок обновлений

    def _abs(self, url: str) -> str:
        return url if url.startswith("http") else f"{self.client.base_url}{url}"

    async def process_updates(self, s) -> tuple[int, int]:
        """Блок «Обновления» на главной — единственный источник событий (F13). Курсора нет: блок хранит
        неделю по дням, каждое событие сверяется с базой, и повторный разбор ничего не дублирует.
        Ленты разделов для этого не годятся: они не упорядочены по выходу серий (F9 опровергнут)."""
        items = parse_updates(await self.client.home())
        if not items:
            log.error("Блок обновлений на главной: 0 событий — вёрстка изменилась?")
            return 0, 0
        await svc.meta_set(s, "updates_ok_at", svc.now().isoformat())
        await svc.meta_set(s, "updates_events", str(len(items)))
        fresh_from = svc.now().date() - timedelta(days=1)          # «Сегодня» и «Вчера»
        self._reads_left = EVENT_READS
        new_eps = queued = deferred = failed = 0
        for item in reversed(items):                                # от старых к новым
            fresh = item.day is not None and item.day >= fresh_from
            try:
                # Точка сохранения на событие: одна кривая страница не откатывает весь проход.
                async with s.begin_nested():
                    e, q = await self._apply_update(s, item, fresh)
            except Deferred:
                deferred += 1
                continue
            except AccessBlocked:
                raise
            except Exception:
                failed += 1
                log.exception("Событие блока обновлений пропущено: %s s%se%s", item.hdrezka_id, item.season, item.episode)
                continue
            new_eps, queued = new_eps + e, queued + q
        if deferred:
            log.info("Блок обновлений: отложено до следующего цикла событий %s — кончился лимит чтения страниц", deferred)
        await svc.meta_set(s, "updates_failed", str(failed))      # видно в проверке здоровья, а не только в логе
        return new_eps, queued

    async def _apply_update(self, s, item: UpdateItem, fresh: bool) -> tuple[int, int]:
        """Одно событие блока → (новых серий, уведомлений)."""
        page = await svc.page_by_hid(s, item.hdrezka_id)
        if page is None:
            page = await svc.upsert_page_from_update(s, item, self._abs(item.url))
        eid = await svc.find_episode(s, page.id, item.season, item.episode)
        page_max = await svc.max_recorded_episode(s, page.id)
        kind = svc.classify_update(eid is not None, page_max, item.season, item.episode, fresh)

        # Классификация уже решена и от чтения не зависит. Непрочитанную страницу с новой серией читаем
        # до уведомлений — ради привязки к франшизе и списка озвучек.
        if kind == "new" and page.page_refreshed_at is None:
            await self._read(s, page, required=True)
        tid = match_voice(await svc.voice_names(s, page.id), item.voice)
        stale = page.page_refreshed_at is None or svc.now() - page.page_refreshed_at > VOICE_REREAD
        if kind != "catchup" and item.voice and tid is None and stale and await self._read(s, page, required=False):
            tid = match_voice(await svc.voice_names(s, page.id), item.voice)

        if eid is None:
            eid = await svc.record_episode(s, page, item.season, item.episode)
            if eid is None:                              # серию уже записали в этом же проходе
                return 0, 0
        new_eps = queued = 0
        if kind == "new":
            if (item.season, item.episode) > (page.last_season or 0, page.last_episode or 0):
                page.last_season, page.last_episode = item.season, item.episode
            page.is_finished, page.last_event_at = False, svc.now()
            new_eps = 1
            n = await svc.enqueue_episode(s, page, eid)
            queued += n
            log.info("НОВАЯ СЕРИЯ: %s s%se%s (%s) → %s уведомлений",
                     page.title, item.season, item.episode, item.voice or "без озвучки", n)
            if page_max is None and page.franchise_id and svc.is_recent_part(page):
                # Первая серия части франшизы — «новая часть» для подписанных на другие её части;
                # подписчикам франшизы хватает уведомления о самой серии.
                queued += await svc.enqueue_new_part(s, page.franchise_id, page, only_page_subscribers=True)
        if tid is not None and await svc.mark_voice_seen(s, eid, tid) and kind != "catchup":
            n = await svc.enqueue_voice(s, page, eid, tid)
            queued += n
            if n:
                log.info("ОЗВУЧКА: %s s%se%s в «%s» → %s уведомлений", page.title, item.season, item.episode, item.voice, n)
        return new_eps, queued

    # ------------------------------------------------------------------ 2: франшизы

    async def refresh_franchises(self, s) -> int:
        # Франшизы с подписчиками, а также с «ждущими продолжения» на завершённых частях:
        # франшизу мог завести бот по ссылке (без перевода ждущих) — сверка переведёт их за сутки.
        rows = (await s.execute(text("""
            SELECT f.id FROM franchises f
             WHERE (EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.franchise_id = f.id)
                    OR EXISTS (SELECT 1 FROM subscriptions sub JOIN pages p ON p.id = sub.page_id
                                WHERE p.franchise_id = f.id AND p.is_finished))
               AND (f.refreshed_at IS NULL OR f.refreshed_at < now() - make_interval(hours => :h))
             ORDER BY f.id LIMIT :lim"""), {"h": FRANCHISE_REFRESH_HOURS, "lim": PER_CYCLE_FRANCHISES})).all()
        done = 0
        for (fid,) in rows:
            anchor = await svc.franchise_anchor(s, fid)
            fr = await s.get(Franchise, fid)
            if not anchor:
                fr.refreshed_at = svc.now()
                continue
            try:
                await self.sync(s, anchor.hdrezka_id, anchor.url)
                done += 1
            except AccessBlocked as exc:
                log.warning("Сверка франшизы %s: %s", fid, exc)
        return done

    # ------------------------------------------------------------------ 3: страницы с подписчиками

    async def refresh_pages(self, s) -> int:
        rows = (await s.execute(text("""
            SELECT DISTINCT p.id FROM pages p
              LEFT JOIN subscriptions sp ON sp.page_id = p.id
              LEFT JOIN subscriptions sf ON sf.franchise_id = p.franchise_id
             WHERE (sp.id IS NOT NULL OR sf.id IS NOT NULL)
               AND (NOT p.is_finished OR sp.id IS NOT NULL)    -- завершённые с «жду продолжения» — раз в неделю
               AND p.url <> ''
               AND coalesce(p.content_type, 'series') = 'series'
               AND (p.page_refreshed_at IS NULL OR p.page_refreshed_at < now() - make_interval(days => :d))
             ORDER BY p.id LIMIT :lim"""), {"d": PAGE_REFRESH_DAYS, "lim": PER_CYCLE_PAGES})).all()
        done = 0
        for (pid,) in rows:
            page = await s.get(Page, pid)
            try:
                await self.sync(s, page.hdrezka_id, page.url)
                done += 1
            except AccessBlocked as exc:
                log.warning("Обновление страницы %s: %s", page.hdrezka_id, exc)
        return done

    # ------------------------------------------------------------------ 4: очередь чтения каталога

    async def read_pending_pages(self, s) -> int:
        """Читает страницы, которых ещё не читали: статус, франшиза, озвучки, расписание, постер.
        Одна и та же битая страница не должна держать очередь: после N неудач откладываем её."""
        done = 0
        for page in await svc.pages_to_read(s, PER_CYCLE_READS):
            try:
                await self.sync(s, page.hdrezka_id, page.url)
                self._read_failures.pop(page.hdrezka_id, None)
                done += 1
            except AccessBlocked as exc:
                n = self._read_failures[page.hdrezka_id] = self._read_failures.get(page.hdrezka_id, 0) + 1
                if n >= READ_GIVE_UP_AFTER:
                    page.page_refreshed_at = svc.now()   # выходим из очереди; недельное обновление вернёт, если есть подписчики
                    self._read_failures.pop(page.hdrezka_id, None)
                    log.warning("Страница %s не читается %s раза подряд — откладываю: %s", page.hdrezka_id, n, exc)
                else:
                    log.warning("Очередь чтения, страница %s: %s", page.hdrezka_id, exc)
                break   # возможно, проблема общая — не тратим остальные слоты этого цикла
        return done

    # ------------------------------------------------------------------ цикл

    async def cycle(self) -> None:
        async with session() as s:
            self._cycle_new_parts = 0
            new_eps, queued = await self.process_updates(s)
            # События и уведомления фиксируем сразу: ниже сверки и чтение страниц ходят на сайт минуты,
            # и откат не должен забирать с собой вышедшие серии.
            await s.commit()
            fr = await self.refresh_franchises(s)
            pg = await self.refresh_pages(s)
            rd = await self.read_pending_pages(s)
            await svc.meta_set(s, "last_poll_ok", svc.now().isoformat())
            await s.commit()
        queued += self._cycle_new_parts
        if new_eps or queued or fr or pg or rd:
            log.info("Цикл: новых серий=%s, уведомлений=%s, франшиз сверено=%s, страниц обновлено=%s, прочитано новых=%s",
                     new_eps, queued, fr, pg, rd)

    async def run(self) -> None:
        await init_db()
        # Один поллер на базу: второй экземпляр (например, при обновлении) ждёт, а не дублирует.
        lock_conn = await engine.connect()
        while not await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}):
            log.warning("Другой поллер держит лок — жду 30 с")
            await asyncio.sleep(30)

        log.info("Поллер запущен: интервал=%sс", cfg.poll_interval)
        failures = 0
        try:
            while True:
                try:
                    await self.cycle()
                    failures = 0
                except AccessBlocked as exc:
                    failures += 1
                    # Не долбимся: временный бан легко превратить в постоянный.
                    pause = min(60 * 2 ** failures, 1800)
                    log.error("Доступ потерян (%s), неудач подряд: %s, пауза %sс", exc, failures, pause)
                    await asyncio.sleep(pause)
                except Exception:
                    failures += 1
                    log.exception("Ошибка цикла")
                await asyncio.sleep(cfg.poll_interval)
        finally:
            await self.client.close()
            await lock_conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Poller().run())


if __name__ == "__main__":
    main()
