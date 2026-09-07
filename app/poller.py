"""Поллер: единственный процесс, который ходит на сайт по расписанию.

Потоки (docs/ARCHITECTURE.md, §4.1):
  1. лента: 3 раздела × стр. 1 (глубже — только пока не найден курсор);
  2. новые страницы из ленты → чтение → франшиза → уведомления «новая часть»;
  3. отложенные проверки озвучек (ajax);
  4. суточная сверка состава франшиз с подписчиками;
  5. недельное обновление страниц с подписчиками (озвучки, расписание).

Число запросов к сайту не зависит от числа пользователей.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, text

from app import service as svc
from app.config import cfg
from app.db import engine, init_db, session
from app.models import Episode, Franchise, Page, VoiceCheck
from app.rezka.client import AccessBlocked, RezkaClient
from app.rezka.parser import FeedItem, parse_episodes_html, parse_feed

log = logging.getLogger("poller")

ADVISORY_LOCK_KEY = 0x52455A4B          # "REZK" — ровно один поллер на базу
FEED_MAX_PAGES = 5
FRANCHISE_REFRESH_HOURS = 24
PAGE_REFRESH_DAYS = 7
PER_CYCLE_FRANCHISES = 2                # чтобы обход не растягивался: остальное — в следующий цикл
PER_CYCLE_PAGES = 2
PER_CYCLE_READS = 8                     # очередь чтения каталога: ~140/ч в пике; при 3 очередь не убывала —
                                        # части франшиз и выдача поиска прибывали быстрее (06.09.2026)
READ_GIVE_UP_AFTER = 3                  # страница не читается N циклов подряд → откладываем на неделю
PER_CYCLE_VOICE_CHECKS = 5


def _key(item: FeedItem) -> str:
    return f"{item.hdrezka_id}:{item.season}:{item.episode}"


class Poller:
    def __init__(self) -> None:
        self.client = RezkaClient()
        self._read_failures: dict[int, int] = {}
        self._cycle_new_parts = 0               # уведомлений new_part за цикл — для строки лога

    # ------------------------------------------------------------------ helpers

    async def sync(self, s, hdrezka_id: int, url: str) -> svc.SyncResult:
        """Читает страницу и, если франшиза уже была известна, уведомляет о новых частях."""
        res = await svc.sync_page(s, self.client, hdrezka_id, url)
        if res.franchise and res.franchise_was_known and res.new_parts:
            # Подписчик франшизы или любой её части: часть стоит прочитать ради текста уведомления.
            has_subs = await s.scalar(text("""
                SELECT 1 FROM subscriptions sub LEFT JOIN pages p ON p.id = sub.page_id
                 WHERE sub.franchise_id = :f OR p.franchise_id = :f LIMIT 1"""), {"f": res.franchise.id})
            for part in res.new_parts:
                if has_subs and part.url:
                    # Читаем новую часть, чтобы знать тип (фильм/сериал) для текста уведомления.
                    try:
                        await svc.sync_page(s, self.client, part.hdrezka_id, part.url)
                    except AccessBlocked as exc:
                        log.warning("Не прочитал новую часть %s: %s", part.hdrezka_id, exc)
                    n = await svc.enqueue_new_part(s, res.franchise.id, part)
                    self._cycle_new_parts += n
                    log.info("НОВАЯ ЧАСТЬ франшизы «%s»: %s → %s подписчикам",
                             res.franchise.name, part.title, n)
        if res.franchise:
            await self._adopt_waiting(s, res.franchise)
        return res

    async def _adopt_waiting(self, s, fr: Franchise) -> None:
        """«Жду продолжения» (§7, решение 3): подписка на завершённую часть франшизы переводится
        на франшизу, а о частях-продолжениях (не завершены, не старше ждавшейся) уходит new_part.
        Идемпотентно: после перевода ждущих у франшизы не остаётся. Срабатывает и на быстром пути
        (новый сезон пришёл в ленту с 1-й серией, его страница показала старую в блоке франшизы),
        и на запасном (недельное перечитывание завершённой страницы с подписчиками)."""
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

    async def _candidate_for_sync(self, s, item: FeedItem) -> bool:
        """Неизвестная страница стоит одного запроса, если это начало сериала
        (новый сезон появляется в ленте с 1-й серии) или название похоже на
        имя франшизы с подписчиками. Всё остальное подберёт суточная сверка."""
        if item.has_episode and item.episode <= 2:
            return True
        names = await s.execute(text("""
            SELECT DISTINCT f.name FROM franchises f
              JOIN subscriptions sub ON sub.franchise_id = f.id
             WHERE length(f.name) >= 8"""))
        low = item.title.lower()
        return any(low.startswith(n[0].lower()) for n in names)

    # ------------------------------------------------------------------ 1+2: лента

    async def process_feed(self, s, section: str) -> tuple[int, int]:
        cursor_key = f"feed_cursor:{section}"
        cursor = await svc.meta_get(s, cursor_key)

        collected: list[FeedItem] = []
        found_cursor = cursor is None
        for page_no in range(1, FEED_MAX_PAGES + 1):
            items = parse_feed(await self.client.feed(section, page_no))
            if not items:
                log.error("[%s стр.%s] 0 карточек — вёрстка изменилась?", section, page_no)
                break
            for it in items:
                if cursor is not None and _key(it) == cursor:
                    found_cursor = True
                    break
                collected.append(it)
            if found_cursor or cursor is None:
                break
        if not found_cursor:
            log.warning("[%s] курсор %s не найден на %s страницах — обработал всё", section, cursor, FEED_MAX_PAGES)

        new_eps = queued = 0
        for item in reversed(collected):                       # от старых к новым
            known = await svc.page_by_hid(s, item.hdrezka_id) is not None
            # Каталог-first: каждая карточка ленты попадает в pages. Страницу читаем сразу только
            # если это важно для уведомлений (начало сезона, похоже на франшизу с подписчиками),
            # остальное дочитает очередь (read_pending_pages) по несколько страниц за цикл.
            page = await svc.upsert_page_from_feed(s, item)
            if not known and await self._candidate_for_sync(s, item):
                try:
                    await self.sync(s, item.hdrezka_id, item.url)
                except AccessBlocked as exc:
                    log.warning("Не прочитал новую страницу %s: %s", item.hdrezka_id, exc)

            if not item.has_episode:
                continue
            episode_id = await svc.record_episode(s, page, item.season, item.episode)
            if episode_id is None:
                continue
            new_eps += 1
            page.last_event_at = svc.now()
            n = await svc.enqueue_episode(s, page, episode_id)
            queued += n
            queued += await self._handle_voices(s, page, episode_id)
            log.info("НОВАЯ СЕРИЯ: %s s%se%s → %s уведомлений", item.title, item.season, item.episode, n)

        if collected:
            await svc.meta_set(s, cursor_key, _key(collected[0]))
        return new_eps, queued

    async def _handle_voices(self, s, page: Page, episode_id: int) -> int:
        """Подписчикам с фильтром озвучек: проверяем сразу, иначе — отложенная проверка."""
        wanted = await svc.wanted_translators(s, page)
        queued = 0
        ep = await s.get(Episode, episode_id)
        for tid in wanted:
            try:
                have = parse_episodes_html(await self.client.episodes_html(page.hdrezka_id, tid))
            except AccessBlocked as exc:
                log.warning("ajax озвучки %s для %s: %s", tid, page.hdrezka_id, exc)
                await svc.schedule_voice_check(s, episode_id, tid, 0)
                continue
            if (ep.season, ep.episode) in have:
                await svc.mark_voice_seen(s, episode_id, tid)
                queued += await svc.enqueue_voice(s, page, episode_id, tid)
            else:
                await svc.schedule_voice_check(s, episode_id, tid, 0)
        return queued

    # ------------------------------------------------------------------ 3: отложенные озвучки

    async def process_voice_checks(self, s) -> int:
        due = (await s.execute(
            select(VoiceCheck).where(VoiceCheck.next_check_at <= svc.now())
            .order_by(VoiceCheck.next_check_at).limit(PER_CYCLE_VOICE_CHECKS))).scalars().all()
        queued = 0
        for vc in due:
            ep = await s.get(Episode, vc.episode_id)
            page = await s.get(Page, ep.page_id) if ep else None
            if not page:
                await s.delete(vc)
                continue
            try:
                have = parse_episodes_html(await self.client.episodes_html(page.hdrezka_id, vc.translator_id))
            except AccessBlocked as exc:
                log.warning("voice_check %s/%s: %s", page.hdrezka_id, vc.translator_id, exc)
                await svc.schedule_voice_check(s, vc.episode_id, vc.translator_id, vc.attempts + 1)
                continue
            if (ep.season, ep.episode) in have:
                await svc.mark_voice_seen(s, vc.episode_id, vc.translator_id)
                queued += await svc.enqueue_voice(s, page, vc.episode_id, vc.translator_id)
                await s.delete(vc)
            else:
                await svc.schedule_voice_check(s, vc.episode_id, vc.translator_id, vc.attempts + 1)
        return queued

    # ------------------------------------------------------------------ 4: франшизы

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

    # ------------------------------------------------------------------ 5: страницы

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

    # ------------------------------------------------------------------ 6: очередь чтения каталога

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
                    log.warning("Страница %s не читается %s цикла подряд — откладываю: %s", page.hdrezka_id, n, exc)
                else:
                    log.warning("Очередь чтения, страница %s: %s", page.hdrezka_id, exc)
                break   # возможно, проблема общая — не тратим остальные слоты этого цикла
        return done

    # ------------------------------------------------------------------ цикл

    async def cycle(self) -> None:
        async with session() as s:
            new_eps = queued = 0
            self._cycle_new_parts = 0
            for section in cfg.feed_sections:
                e, q = await self.process_feed(s, section)
                new_eps += e
                queued += q
            queued += await self.process_voice_checks(s)
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

        log.info("Поллер запущен: разделы=%s, интервал=%sс", cfg.feed_sections, cfg.poll_interval)
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
