"""Поллер: единственный процесс, который ходит на сайт по расписанию.

Потоки (docs/ARCHITECTURE.md, §4.1):
  1. блок «Обновления» на главной — единственный источник событий: новые серии и озвучки (F13);
     сразу за ним — уведомления о новых частях франшиз из их состава (franchise_members);
  2. суточная сверка состава франшиз с подписчиками: фильмы и спин-оффы, которых в блоке нет;
  3. перечитывание страниц с подписчиками: расписание, озвучки, статус — выходящие дважды в сутки,
     завершённые («жду продолжения») раз в неделю; заодно подстраховка, если серия прошла мимо блока;
  4. очередь чтения каталога: страницы, которых ещё не читали.

Число запросов к сайту не зависит от числа пользователей.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, text

from app import lifecycle
from app import service as svc
from app.config import cfg
from app.db import init_db, session
from app.models import Episode, Franchise, Page
from app.rezka.client import AccessBlocked, PageGone, RezkaClient
from app.rezka.parser import UpdateItem, match_voice, parse_updates

log = logging.getLogger("poller")

WATCHDOG_LIMIT = 30 * 60                # цикл не отмечался 30 мин — процесс завершается, Docker перезапускает
EVENT_READS = 10                        # чтений страниц ради событий за цикл; не хватило — событие ждёт цикла
VOICE_REREAD = timedelta(hours=24)      # незнакомая озвучка → перечитать список озвучек страницы, не чаще
FRANCHISE_REFRESH_HOURS = 24
PAGE_REFRESH_HOURS = 12                 # выходящие страницы с подписчиками: расписание сайта живое
PAGE_REFRESH_DAYS = 7                   # завершённые, на которых ждут продолжения
PER_CYCLE_FRANCHISES = 2                # чтобы обход не растягивался: остальное — в следующий цикл
PER_CYCLE_PAGES = 2
PER_CYCLE_READS = 8                     # очередь чтения каталога
PER_CYCLE_ANNOUNCE = 5                  # новых частей франшиз за цикл
READ_GIVE_UP_AFTER = 3                  # страница не читается N раз подряд → событие идёт без чтения
REFRESH_CANDIDATES = 3                  # кандидатов на единицу квоты: неудачные не съедают слоты живых


class Deferred(Exception):
    """Событию нужно чтение страницы, а лимит цикла исчерпан или страница не прочиталась. Разберём
    в следующем цикле: блок хранит событие неделю, а от этой попытки в базе ничего не осталось — транзакция
    события откатилась. read_failed — неудачу чтения записываем уже после отката."""

    def __init__(self, read_failed: bool = False) -> None:
        super().__init__()
        self.read_failed = read_failed


class Poller:
    def __init__(self, client: RezkaClient | None = None) -> None:
        self.client = client or RezkaClient()
        self._failed_now: set[int] = set()      # страницы, не прочитавшиеся в этом цикле: второй раз не просим
        self._refresh_failed = 0                # единиц второй части цикла с ошибкой — в meta и /stats
        self.watchdog: lifecycle.Watchdog | None = None
        self._cycle_new_parts = 0               # уведомлений new_part за цикл — для строки лога
        self._reads_left = EVENT_READS          # на цикл: блок обновлений, новые части; сбрасывает cycle()

    # ------------------------------------------------------------------ чтение страниц

    async def sync(self, s, hdrezka_id: int, url: str) -> svc.SyncResult:
        """Читает страницу. Новые части франшизы сервис дописывает в состав, уведомляет announce_new_parts."""
        res = await svc.sync_page(s, self.client, hdrezka_id, url)
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
        by_user: dict[int, list[tuple[Page, list[int] | None]]] = {}
        for user_id, page_id, voices in waiting:
            if page_id in by_id:
                by_user.setdefault(user_id, []).append((by_id[page_id], voices))
        moved = queued = 0
        for user_id, waited_list in by_user.items():
            for waited, _ in waited_list:
                for part in svc.continuation_parts(parts, waited):
                    if part.page_refreshed_at is None and part.url:      # тип и статус части — со страницы
                        try:
                            await svc.sync_page(s, self.client, part.hdrezka_id, part.url)
                        except (AccessBlocked, PageGone) as exc:
                            log.warning("Не прочитал часть %s для ждущего продолжения: %s", part.hdrezka_id, exc)
            # Выбранная озвучка переходит в подписку на франшизу; несколько ждавших — объединение (24.09.2026).
            voices = svc.merge_voice_filters(*(v for _, v in waited_list))
            await svc.subscribe_franchise(s, user_id, fr.id, voices=voices)   # удаляет и подписки на страницы
            moved += 1
            for waited, _ in waited_list:
                for part in svc.continuation_parts(parts, waited):
                    queued += await svc.enqueue_new_part(s, fr.id, part, user_id=user_id)
        self._cycle_new_parts += queued
        log.info("ПРОДОЛЖЕНИЕ: франшиза «%s» — переведено подписок %s, уведомлений %s", fr.name, moved, queued)

    async def _read(self, s, page: Page, required: bool) -> bool:
        """Чтение страницы ради события. Обязательное — новая серия на непрочитанной странице: без привязки
        к франшизе её подписчики уведомления не получат; при исчерпанном лимите или неудаче событие
        откладывается до следующего цикла, после READ_GIVE_UP_AFTER неудач подряд — идёт без чтения, пока не
        подойдёт время следующей попытки. Часовых пауз очередей обновления событие не ждёт: премьера после одного
        сбоя чтения приходила через час, а не через цикл (25.09.2026). Необязательное — обновить список озвучек:
        берёт не больше половины лимита, ждёт паузу после неудач и просто пропускается. Неудачи — в базе (pages)."""
        paused = page.next_read_at is not None and page.next_read_at > svc.now()
        if required and page.read_failures >= READ_GIVE_UP_AFTER and paused:
            return False
        if page.id in self._failed_now or (paused and not required) \
                or self._reads_left <= (0 if required else EVENT_READS // 2):
            if required:
                raise Deferred()
            return False
        self._reads_left -= 1
        try:
            await self.sync(s, page.hdrezka_id, page.url)
            return True
        except PageGone as exc:
            log.warning("Страница %s пропала с сайта, событие без чтения: %s", page.hdrezka_id, exc)
            await svc.mark_read_failure(s, page.id, gone=True)
            self._failed_now.add(page.id)
            return False
        except AccessBlocked as exc:
            self._failed_now.add(page.id)
            if required and page.read_failures + 1 < READ_GIVE_UP_AFTER:
                log.warning("Страница %s не прочиталась ради события, повторю в следующем цикле: %s", page.hdrezka_id, exc)
                raise Deferred(read_failed=True) from exc
            log.warning("Страница %s не прочиталась ради события, иду дальше без неё: %s", page.hdrezka_id, exc)
            await svc.mark_read_failure(s, page.id, gone=False)
            return False

    # ------------------------------------------------------------------ 1: блок обновлений

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
        await s.commit()
        fresh_from = svc.now().date() - timedelta(days=1)          # «Сегодня» и «Вчера»
        new_eps = queued = deferred = failed = 0
        for item in reversed(items):                                # от старых к новым
            if lifecycle.stopping():
                break                                               # остальное — в следующем запуске: блок хранит неделю
            self._beat()
            fresh = item.day is not None and item.day >= fresh_from
            # Событие — своей транзакцией: одна кривая страница не откатывает весь проход, а блокировки строк (чтение
            # страницы блокирует все части франшизы) держатся одно событие, а не весь проход — бот не ждёт (25.09.2026).
            try:
                e, q = await self._apply_update(s, item, fresh)
                await s.commit()
            except Deferred as d:
                await s.rollback()
                deferred += 1
                if d.read_failed:                       # попытку откатили — неудачу чтения пишем отдельно
                    await self._mark_event_read_failure(s, item)
                    await s.commit()
                continue
            except AccessBlocked:
                await s.rollback()
                raise
            except Exception:
                await s.rollback()
                failed += 1
                log.exception("Событие блока обновлений пропущено: %s s%se%s", item.hdrezka_id, item.season, item.episode)
                continue
            new_eps, queued = new_eps + e, queued + q
        if deferred:
            log.info("Блок обновлений: отложено до следующего цикла событий %s — лимит чтения страниц или неудачное чтение",
                     deferred)
        await svc.meta_set(s, "updates_failed", str(failed))      # видно в проверке здоровья, а не только в логе
        return new_eps, queued

    async def _mark_event_read_failure(self, s, item: UpdateItem) -> None:
        """Неудача чтения ради события — после отката его транзакции. Страницу, которую завело само событие,
        заводим заново: иначе отметка ушла бы вместе с ней, счётчик неудач не рос, и событие про новый тайтл,
        который не читается, откладывалось бы каждый цикл, пока блок его хранит (25.09.2026)."""
        page = await svc.page_by_hid(s, item.hdrezka_id) or await svc.upsert_page_from_update(s, item, item.url)
        await svc.mark_read_failure(s, page.id, gone=False)
        self._failed_now.add(page.id)

    async def _apply_update(self, s, item: UpdateItem, fresh: bool) -> tuple[int, int]:
        """Одно событие блока → (новых серий, уведомлений)."""
        page = await svc.page_by_hid(s, item.hdrezka_id)
        if page is None:
            page = await svc.upsert_page_from_update(s, item, item.url)
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

    # ------------------------------------------------------------------ 1б: новые части франшиз

    PENDING_SQL = text("""
        SELECT m.franchise_id, m.hdrezka_id FROM franchise_members m
          JOIN pages p ON p.hdrezka_id = m.hdrezka_id
         WHERE m.announce = 'pending' AND p.url <> ''   -- анонс без страницы ждёт, пока она появится
         ORDER BY m.seen_at, m.hdrezka_id LIMIT :lim""")

    async def announce_new_parts(self) -> int:
        """Новые части франшиз — фильмы, спин-оффы, сезоны. В состав (franchise_members, pending) их дописывает
        любое чтение страницы, бота или поллера; уведомляет только поллер, здесь. Старые части — молча
        (skipped). Сериал, у которого уже есть серии, — только подписчикам других частей: подписчики
        франшизы знают о нём по уведомлениям о сериях. Дубли отсекает uq_notification.
        Каждая часть — отдельно: неожиданная ошибка одной — лог и следующая. Раньше она роняла весь цикл,
        вторая его часть не выполнялась ни разу, а проверка здоровья этого не видела (25.09.2026)."""
        async with session() as s:
            rows = (await s.execute(self.PENDING_SQL, {"lim": PER_CYCLE_ANNOUNCE})).all()
        queued = 0
        for fid, hid in rows:
            if lifecycle.stopping():
                break
            self._beat()
            try:
                queued += await self._announce_part(fid, hid)
            except Exception:
                self._refresh_failed += 1
                log.exception("Новая часть %s франшизы %s: объявление не удалось — повторю в следующем цикле", hid, fid)
        return queued

    async def _announce_part(self, fid: int, hid: int) -> int:
        """Одна новая часть: тип дочитываем вне транзакции (_refresh_unit), объявление — короткой транзакцией."""
        async with session() as s:
            page = await svc.page_by_hid(s, hid)
            pid, url, ctype = page.id, page.url, page.content_type
            waiting = page.next_read_at is not None and page.next_read_at > svc.now()
        if ctype is None and not waiting:                  # тип части — для текста и решения, кому писать
            if self._reads_left <= 0:
                return 0                                   # лимит чтений цикла — дочитаем в следующем
            self._reads_left -= 1
            try:
                await self._refresh_unit(hid, url, pid)    # не прочиталась — неудача отмечена, сообщим без типа
            except AccessBlocked as exc:
                self._reads_left = 0                       # доступа нет — в этом цикле больше не читаем
                log.warning("Не прочитал новую часть %s: %s — сообщу без типа", hid, exc)
        async with session() as s:
            page = await svc.page_by_hid(s, hid)
            n, status = 0, "skipped"
            if svc.is_recent_part(page):
                has_episodes = await s.scalar(select(Episode.id).where(Episode.page_id == page.id).limit(1)) is not None
                n = await svc.enqueue_new_part(s, fid, page, only_page_subscribers=has_episodes)
                status = "sent"
                log.info("НОВАЯ ЧАСТЬ франшизы %s: %s → %s уведомлений", fid, page.title, n)
            await s.execute(text("UPDATE franchise_members SET announce = :st WHERE franchise_id = :f AND hdrezka_id = :h"),
                            {"st": status, "f": fid, "h": hid})
            await s.commit()
        return n

    # ------------------------------------------------------------------ 2–4: по одной единице

    async def _refresh_unit(self, hdrezka_id: int, url: str, page_id: int, after=None):
        """Одна страница вне цикла событий: запрос к сайту — без открытой транзакции, затем короткая запись
        и коммит. Ошибка одной единицы не откатывает остальные (24.09.2026: раньше вся вторая часть цикла
        шла одной транзакцией). PageGone и другие ошибки страницы — отметка с растущей паузой и None;
        AccessBlocked — отметка и дальше вызывающему: доступ потерян, часть цикла заканчивается."""
        self._beat()
        try:
            tp = await svc.fetch_title_page(self.client, url)
        except PageGone as exc:
            log.warning("Страница %s пропала с сайта: %s", hdrezka_id, exc)
            await self._mark(page_id, gone=True)
            return None
        except AccessBlocked:
            await self._mark(page_id, gone=False)
            raise
        except Exception:
            log.exception("Страница %s: ошибка чтения", hdrezka_id)
            self._refresh_failed += 1
            await self._mark(page_id, gone=False)
            return None
        try:
            async with session() as s:
                res = await svc.apply_title_page(s, hdrezka_id, url, tp)
                if res.franchise:
                    await self._adopt_waiting(s, res.franchise)
                if after:
                    await after(s, res)
                await s.commit()
            return res
        except Exception:
            log.exception("Страница %s: ошибка записи", hdrezka_id)
            self._refresh_failed += 1
            await self._mark(page_id, gone=False)
            return None

    async def _mark(self, page_id: int, gone: bool) -> None:
        async with session() as s:
            await svc.mark_read_failure(s, page_id, gone)
            await s.commit()

    def _beat(self) -> None:
        if self.watchdog:
            self.watchdog.beat()

    async def refresh_franchises(self) -> int:
        # Франшизы с подписчиками, а также с «ждущими продолжения» на завершённых частях:
        # франшизу мог завести бот по ссылке (без перевода ждущих) — сверка переведёт их за сутки.
        async with session() as s:
            rows = (await s.execute(text("""
                SELECT f.id FROM franchises f
                 WHERE (EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.franchise_id = f.id)
                        OR EXISTS (SELECT 1 FROM subscriptions sub JOIN pages p ON p.id = sub.page_id
                                    WHERE p.franchise_id = f.id AND p.is_finished))
                   AND (f.refreshed_at IS NULL OR f.refreshed_at < now() - make_interval(hours => :h))
                   AND (f.next_refresh_at IS NULL OR f.next_refresh_at <= now())
                 ORDER BY f.refreshed_at NULLS FIRST, f.id LIMIT :lim"""),
                {"h": FRANCHISE_REFRESH_HOURS, "lim": PER_CYCLE_FRANCHISES})).scalars().all()
        done = 0
        for fid in rows:
            if lifecycle.stopping():
                break
            async with session() as s:
                anchor = await svc.franchise_anchor(s, fid)
                if anchor is None:                       # читать нечего: все части пропали или ждут попытки
                    await s.execute(text("UPDATE franchises SET refreshed_at = now() WHERE id = :f"), {"f": fid})
                    await s.commit()
                    continue
                hid, url, pid = anchor.hdrezka_id, anchor.url, anchor.id
            try:
                res = await self._refresh_unit(hid, url, pid)
            except AccessBlocked as exc:
                log.warning("Сверка франшизы %s: %s", fid, exc)
                async with session() as s:
                    await svc.mark_refresh_failure(s, fid)
                    await s.commit()
                raise
            if res is not None:
                async with session() as s:
                    await s.execute(text("UPDATE franchises SET refresh_failures = 0, next_refresh_at = NULL,"
                                         " refreshed_at = now() WHERE id = :f"), {"f": fid})
                    await s.commit()
                done += 1
            # Не прочиталась — якорь помечен, в следующем цикле сверка пойдёт по другой части.
        return done

    # ------------------------------------------------------------------ 3: страницы с подписчиками

    REFRESH_SQL = text("""
        SELECT p.id FROM pages p
         WHERE p.url <> '' AND coalesce(p.content_type, 'series') = 'series'
           AND (p.next_read_at IS NULL OR p.next_read_at <= now())
           AND (EXISTS (SELECT 1 FROM subscriptions sp WHERE sp.page_id = p.id)
                OR (NOT p.is_finished AND EXISTS (SELECT 1 FROM subscriptions sf WHERE sf.franchise_id = p.franchise_id)))
           AND (p.page_refreshed_at IS NULL OR p.page_refreshed_at < now() - CASE WHEN p.is_finished
                    THEN make_interval(days => :d) ELSE make_interval(hours => :h) END)
         ORDER BY p.page_refreshed_at NULLS FIRST LIMIT :lim""")

    async def refresh_pages(self) -> int:
        """Страницы с подписчиками: выходящие — раз в 12 часов, завершённые («жду продолжения») — раз в неделю.
        EXISTS вместо двух LEFT JOIN: те перемножали подписки на страницу и на франшизу (300 × 300 = 90 000 строк)."""
        async with session() as s:
            ids = (await s.execute(self.REFRESH_SQL, {"d": PAGE_REFRESH_DAYS, "h": PAGE_REFRESH_HOURS,
                                                      "lim": PER_CYCLE_PAGES * REFRESH_CANDIDATES})).scalars().all()
        done = 0
        for pid in ids:
            if done >= PER_CYCLE_PAGES or lifecycle.stopping():
                break
            async with session() as s:
                page = await s.get(Page, pid)
                hid, url = page.hdrezka_id, page.url
            try:
                res = await self._refresh_unit(hid, url, pid, after=self._catch_missed)
            except AccessBlocked as exc:
                log.warning("Обновление страницы %s: %s", hid, exc)
                raise
            done += res is not None
        return done

    async def _catch_missed(self, s, res: svc.SyncResult) -> int:
        """Подстраховка на случай, если серия не попала в блок обновлений: список серий на самой странице
        новее всего, что бот записал, — значит серия на сайте есть, а события не было. Работает только для
        страниц с подписчиками (их и перечитываем) и только когда записи уже есть: без них не с чем
        сравнивать, и первое чтение каталога подняло бы весь сезон."""
        page = res.page
        if not res.episodes:
            return 0
        top_season = max(res.episodes)
        top = (top_season, max(res.episodes[top_season]))
        page_max = await svc.max_recorded_episode(s, page.id)
        if page_max is None or top <= page_max:
            return 0
        eid = await svc.record_episode(s, page, *top)
        if eid is None:
            return 0
        page.last_season, page.last_episode = top
        page.is_finished, page.last_event_at = False, svc.now()
        n = await svc.enqueue_episode(s, page, eid)
        # Список серий на странице — озвучки по умолчанию: подписчикам именно этой озвучки тоже пора.
        if page.default_translator and await svc.mark_voice_seen(s, eid, page.default_translator):
            n += await svc.enqueue_voice(s, page, eid, page.default_translator)
        log.warning("СЕРИЯ МИМО БЛОКА ОБНОВЛЕНИЙ: %s s%se%s → %s уведомлений",
                    page.title, top[0], top[1], n)
        return n

    # ------------------------------------------------------------------ 4: очередь чтения каталога

    async def read_pending_pages(self) -> int:
        """Читает страницы, которых ещё не читали: статус, франшиза, озвучки, расписание, постер.
        Неудачная страница ждёт своего времени (pages.next_read_at) и не держит очередь."""
        async with session() as s:
            pages = [(p.id, p.hdrezka_id, p.url) for p in await svc.pages_to_read(s, PER_CYCLE_READS)]
        done = 0
        for pid, hid, url in pages:
            if lifecycle.stopping():
                break
            try:
                res = await self._refresh_unit(hid, url, pid)
            except AccessBlocked as exc:
                log.warning("Очередь чтения, страница %s: %s", hid, exc)
                raise
            done += res is not None
        return done

    # ------------------------------------------------------------------ цикл

    async def cycle(self) -> None:
        self._cycle_new_parts = 0
        self._reads_left = EVENT_READS
        self._failed_now = set()
        self._refresh_failed = 0
        async with session() as s:
            new_eps, queued = await self.process_updates(s)
            # События и уведомления фиксируем сразу: дальше сверки и чтение страниц ходят на сайт минуты.
            await svc.meta_set(s, "last_poll_ok", svc.now().isoformat())
            await s.commit()
        queued += await self.announce_new_parts()
        # Вторая часть — по одной единице, у каждой своя короткая транзакция. Потеря доступа заканчивает её
        # до следующего цикла; ошибка одной страницы — только её.
        fr = pg = rd = 0
        try:
            fr = await self.refresh_franchises()
            pg = await self.refresh_pages()
            rd = await self.read_pending_pages()
            refreshed = True
        except AccessBlocked:
            refreshed = False
        async with session() as s:
            await svc.meta_set(s, "refresh_failed", str(self._refresh_failed))
            if refreshed:
                await svc.meta_set(s, "refresh_ok_at", svc.now().isoformat())
            await s.commit()
            await lifecycle.ping_if_healthy(s)
        queued += self._cycle_new_parts
        if new_eps or queued or fr or pg or rd:
            log.info("Цикл: новых серий=%s, уведомлений=%s, франшиз сверено=%s, страниц обновлено=%s, прочитано новых=%s",
                     new_eps, queued, fr, pg, rd)

    async def run(self) -> None:
        await init_db()
        lifecycle.install_signal_handlers()
        lock_conn = await acquire_lock()
        self.watchdog = lifecycle.Watchdog("poller", WATCHDOG_LIMIT).start()
        log.info("Поллер запущен: интервал=%sс", cfg.poll_interval)
        failures = 0
        try:
            while not lifecycle.stopping():
                self._beat()
                await check_lock(lock_conn)          # вне try ниже: потеря лока завершает процесс
                try:
                    await self.cycle()
                    failures = 0
                except AccessBlocked as exc:
                    failures += 1
                    # Не долбимся: временный бан легко превратить в постоянный.
                    pause = min(60 * 2 ** failures, 1800)
                    log.error("Доступ потерян (%s), неудач подряд: %s, пауза %sс", exc, failures, pause)
                    await lifecycle.pause(pause, self.watchdog)
                except Exception:
                    failures += 1
                    log.exception("Ошибка цикла")
                await lifecycle.pause(cfg.poll_interval, self.watchdog)
            log.info("Поллер остановлен по сигналу")
        finally:
            self.watchdog.stop()
            await self.client.close()
            await lock_conn.close()


async def acquire_lock():
    """Один поллер на базу: второй экземпляр (например, при обновлении) ждёт, а не дублирует (app/lifecycle.py)."""
    return await lifecycle.acquire_lock(lifecycle.POLLER_LOCK, "поллер")


check_lock = lifecycle.check_lock


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Poller().run())


if __name__ == "__main__":
    main()
