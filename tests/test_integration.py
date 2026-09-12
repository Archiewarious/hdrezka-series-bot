"""Путь события от блока обновлений до уведомления — на настоящем Postgres.

Каждый тест — живой случай 10.09.2026 или защита от него. Сайт подменён: главная и страницы тайтлов
собираются здесь же по той вёрстке, которую разбирает app/rezka/parser.py.
"""
import re
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import select, text

from app import service as svc
from app.db import session
from app.models import Episode, Franchise, Page, Schedule, Subscription, User, Voice
from app.poller import Poller

TODAY = date.today()
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]


def block(days: dict) -> str:
    """{дата: [(hdrezka_id, название, раздел, сезон, серия, озвучка)]} → HTML блока обновлений."""
    out = []
    for d in sorted(days, reverse=True):
        items = "".join(
            '<li class="b-seriesupdate__block_list_item"><div class="b-seriesupdate__block_list_item_inner">'
            f'<div class="cell cell-1"><a class="b-seriesupdate__block_list_link" href="/{sec}/x/{hid}-t.html">{title}</a> '
            f'<span class="season">({se} сезон)</span></div> <span class="cell cell-2">{ep} серия'
            + (f" <i>({voice})</i>" if voice else "") + "</span></div></li>"
            for hid, title, sec, se, ep, voice in days[d])
        out.append(f'<div class="b-seriesupdate__block"><div class="b-seriesupdate__block_date">'
                   f'{d.day} {_MONTHS[d.month - 1]} {d.year}</div><ul class="b-seriesupdate__block_list">{items}</ul></div>')
    return "".join(out)


def title_page(hid: int, title: str, season: int, last_ep: int, voices: list, franchise: list = ()) -> str:
    """voices: [(translator_id, имя, флажок или None)]; franchise: [(hdrezka_id, название, год)]."""
    tr = "".join(f'<li class="b-translator__item" data-translator_id="{t}">{n}'
                 + (f'<img title="{f}" alt="{f}" src="f.png">' if f else "") + "</li>" for t, n, f in voices)
    eps = "".join(f'<li class="b-simple_episode__item" data-season_id="{season}" data-episode_id="{e}"></li>'
                  for e in range(1, last_ep + 1))
    parts = "".join(f'<div class="b-post__partcontent_item" data-url="https://rezka.test/series/y/{h}-p.html">'
                    f'<div class="title">{t}</div><div class="year">{y} год</div></div>' for h, t, y in franchise)
    if franchise:
        parts += (f'<div class="b-post__partcontent_item current"><div class="title">{title}</div>'
                  f'<div class="year">{TODAY.year} год</div></div>')
    return (f'<div class="b-post__title"><h1>{title}</h1></div>'
            f"<script>initCDNSeriesEvents({hid}, {voices[0][0]}, {season}, {last_ep}, false)</script>"
            f"<ul>{tr}</ul><ul>{eps}</ul>{parts}")


class FakeSite:
    base_url = "https://rezka.test"

    def __init__(self, home: str, pages: dict | None = None):
        self._home, self._pages, self.reads = home, pages or {}, []

    async def home(self) -> str:
        return self._home

    async def title_page(self, url: str) -> str:
        hid = int(re.search(r"/(\d+)-", url).group(1))
        self.reads.append(hid)
        return self._pages[hid]

    async def close(self) -> None:
        pass


async def _page(s, hid, title, *, last=None, voices=(), rows=(), read=True, franchise_id=None, finished=False):
    page = Page(hdrezka_id=hid, title=title, url=f"https://rezka.test/series/y/{hid}-p.html", content_type="series",
                last_season=last[0] if last else None, last_episode=last[1] if last else None,
                page_refreshed_at=svc.now() if read else None, franchise_id=franchise_id, is_finished=finished)
    s.add(page)
    await s.flush()
    for tid, name in voices:
        s.add(Voice(page_id=page.id, translator_id=tid, name=name))
    for se, ep in rows:
        s.add(Episode(page_id=page.id, season=se, episode=ep))
    await s.flush()
    return page


async def _run(site: FakeSite) -> tuple[int, int]:
    async with session() as s:
        result = await Poller(site).process_updates(s)
        await s.commit()
    return result


async def _notifications(user_id: int) -> list[tuple]:
    async with session() as s:
        return sorted((await s.execute(text("""
            SELECT n.kind, e.season, e.episode FROM notifications n
              LEFT JOIN episodes e ON e.id = n.ref_id AND n.kind <> 'new_part'
             WHERE n.user_id = :u"""), {"u": user_id})).all(), key=str)


def test_premiere_reaches_franchise_subscriber(db):
    """10.09.2026: чтение страницы, запущенное самим событием, превращало премьеру в «дозвучку»."""
    async def scenario():
        async with session() as s:
            s.add_all([User(id=1), User(id=2)])
            fr = Franchise(key_hdrezka_id=200, name="Сага")
            s.add(fr)
            await s.flush()
            old = await _page(s, 200, "Сага [ТВ-1]", last=(1, 12), rows=[(1, 12)], franchise_id=fr.id)
            await svc.subscribe_franchise(s, 1, fr.id)
            await svc.subscribe_page(s, 2, old.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(300, "Сага [ТВ-2]", "series", 2, 1, "Дубляж")]}),
                        {300: title_page(300, "Сага [ТВ-2]", 2, 1, [(56, "Дубляж", None)], [(200, "Сага [ТВ-1]", 2020)])})
        new, _ = await _run(site)
        return new, site.reads, await _notifications(1), await _notifications(2)

    new, reads, franchise_sub, page_sub = db(scenario)
    assert reads == [300], "непрочитанная страница с новой серией читается до уведомления"
    assert new == 1 and franchise_sub == [("episode", 2, 1)], "подписчик франшизы узнаёт о премьере"
    assert page_sub == [("new_part", None, None)], "подписанный на прошлый сезон — «новая часть»"


def test_waiting_for_continuation_gets_new_part(db):
    """«Жду продолжения»: подписка на завершённый сезон переводится на франшизу, когда выходит продолжение.
    Ждавший получает «новую часть» и первую серию — они создаются вместе и уходят одним дайджестом."""
    async def scenario():
        async with session() as s:
            s.add(User(id=7))
            fr = Franchise(key_hdrezka_id=210, name="Сага")
            s.add(fr)
            await s.flush()
            old = await _page(s, 210, "Сага [ТВ-1]", last=(1, 12), rows=[(1, 12)], franchise_id=fr.id, finished=True)
            await svc.subscribe_page(s, 7, old.id)
            await s.commit()
        site = FakeSite(block({TODAY: [(310, "Сага [ТВ-2]", "series", 2, 1, "Дубляж")]}),
                        {310: title_page(310, "Сага [ТВ-2]", 2, 1, [(56, "Дубляж", None)], [(210, "Сага [ТВ-1]", 2020)])})
        await _run(site)
        async with session() as s:
            scopes = (await s.execute(select(Subscription.scope).where(Subscription.user_id == 7))).scalars().all()
        return scopes, await _notifications(7)

    scopes, notes = db(scenario)
    assert scopes == ["franchise"], "ждавший переведён на франшизу"
    assert notes == [("episode", 2, 1), ("new_part", None, None)]


def test_old_dub_is_silent_new_episode_notifies(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=3))
            page = await _page(s, 400, "Реинкарнация", last=(3, 11), rows=[(3, 11)],
                               voices=[(224, "AniStar"), (477, "DreamCast")])
            await svc.subscribe_page(s, 3, page.id)
            await s.commit()
        site = FakeSite(block({TODAY - timedelta(days=2): [(400, "Реинкарнация", "animation", 3, 3, "DreamCast")],
                               TODAY: [(400, "Реинкарнация", "animation", 3, 12, "AniStar")]}))
        await _run(site)
        async with session() as s:
            row33 = await s.scalar(select(Episode.id).where(Episode.season == 3, Episode.episode == 3))
        return site.reads, await _notifications(3), row33

    reads, notes, row33 = db(scenario)
    assert reads == [] and notes == [("episode", 3, 12)], "дозвучка 3×3 молча, 3×12 — уведомление"
    assert row33 is not None, "старая серия записана, чтобы следующая её озвучка не сошла за новую"


def test_two_dubs_two_notifications(db):
    """Решение пользователя 10.09.2026: две озвучки в подписке — два уведомления, в каждом своя озвучка."""
    async def scenario():
        async with session() as s:
            s.add(User(id=4))
            page = await _page(s, 500, "Крестьянин", last=(1, 11), rows=[(1, 11)],
                               voices=[(56, "Дубляж"), (7, "FanVoxUA (Украинский)")])
            s.add(Subscription(user_id=4, scope="page", page_id=page.id, voice_filter=[56, 7]))
            await s.commit()
        await _run(FakeSite(block({TODAY: [(500, "Крестьянин", "animation", 1, 12, "Дубляж"),
                                           (500, "Крестьянин", "animation", 1, 12, "FanVoxUA (Украинский)")]})))
        return await _notifications(4)

    assert db(scenario) == [("voice:56", 1, 12), ("voice:7", 1, 12)]


def test_ambiguous_dub_is_not_guessed(db):
    """10.09.2026: «Дубляж» из блока отмечал случайного из нескольких «Дубляж (…)» страницы."""
    async def scenario():
        async with session() as s:
            s.add(User(id=5))
            page = await _page(s, 600, "Сериал", last=(1, 11), rows=[(1, 11)],
                               voices=[(1, "Дубляж (HDrezka Studio)"), (2, "Дубляж (TVOË)")])
            s.add(Subscription(user_id=5, scope="page", page_id=page.id, voice_filter=[1]))
            await s.commit()
        site = FakeSite(block({TODAY: [(600, "Сериал", "series", 1, 12, "Дубляж")]}))
        await _run(site)
        async with session() as s:
            marked = (await s.execute(text("SELECT count(*) FROM episode_voices"))).scalar()
        return site.reads, marked, await _notifications(5)

    assert db(scenario) == ([], 0, [])


def test_stale_event_about_unknown_title_is_silent(db):
    async def scenario():
        site = FakeSite(block({TODAY - timedelta(days=3): [(700, "Старое", "series", 1, 5, "Coldfilm")]}))
        new, queued = await _run(site)
        async with session() as s:
            page = await svc.page_by_hid(s, 700)
            row = await s.scalar(select(Episode.id).where(Episode.page_id == page.id))
        return new, queued, site.reads, row

    new, queued, reads, row = db(scenario)
    assert (new, queued, reads) == (0, 0, []) and row is not None


def test_subscription_seeds_known_episode(db):
    """Страница без записей: дозвучка текущей серии после подписки — не «вышла новая серия»."""
    async def scenario():
        async with session() as s:
            s.add(User(id=6))
            page = await _page(s, 800, "Ждун", last=(2, 12), voices=[(19, "AniLibria")], finished=True)
            await svc.subscribe_page(s, 6, page.id)
            await s.commit()
            seeded = (await s.execute(text("SELECT extract(year FROM first_seen_at) FROM episodes"))).scalar()
        await _run(FakeSite(block({TODAY: [(800, "Ждун", "animation", 2, 12, "AniLibria"),
                                           (800, "Ждун", "animation", 2, 5, "AniLibria")]})))
        return seeded, await _notifications(6)

    seeded, notes = db(scenario)
    assert seeded == 1970, "затравка не попадает в «🆕 Новое» и в проверку здоровья"
    assert notes == []


def test_notification_text_names_the_dub(db):
    """Решение 10.09.2026: уведомление подписано озвучкой — и для «любой» (первая вышедшая), и для выбранной."""
    from app.sender import _render

    async def scenario():
        async with session() as s:
            s.add(User(id=8))
            page = await _page(s, 950, "Сериал", last=(1, 3), rows=[(1, 3)],
                               voices=[(56, "Дубляж"), (7, "FanVoxUA (Украинский)")])
            eid = await s.scalar(select(Episode.id).where(Episode.page_id == page.id))
            await svc.mark_voice_seen(s, eid, 7)
            await s.commit()
            any_voice = await _render(s, 8, "episode", eid, "ru")
            chosen = await _render(s, 8, "voice:56", eid, "ru")
        return any_voice.text, chosen.text

    any_text, chosen_text = db(scenario)
    assert any_text == "🎬 <b>Сериал</b>\n\n📺 1 сезон · 3 серия\n🎙 FanVoxUA (Украинский)", "пост: название, серия, озвучка"
    assert "Дубляж" in chosen_text and "FanVoxUA" not in chosen_text


def test_calendar_sql_runs(db):
    """07.09.2026 календарь падал на «date + unknown»: SQL без базы не проверялся."""
    from app.bot.main import CAL_DAYS, CAL_LATE_DAYS, CALENDAR_SQL

    async def scenario():
        async with session() as s:
            return (await s.execute(CALENDAR_SQL, {"uid": 1, "days": CAL_DAYS, "back": CAL_LATE_DAYS})).all()

    assert db(scenario) == []


def test_calendar_hides_episodes_already_out_in_your_dub(db):
    """11.09.2026: календарь показывал «сегодня» и «завтра» серии, которые уже вышли в дубляже и были
    разосланы 10 сентября — расписание сайта отстаёт от загрузок."""
    from app.bot.main import CAL_DAYS, CAL_LATE_DAYS, CALENDAR_SQL

    async def scenario():
        async with session() as s:
            s.add(User(id=9))
            page = await _page(s, 960, "Сериал", last=(1, 11), rows=[(1, 10), (1, 11)],
                               voices=[(56, "Дубляж"), (238, "Оригинал (+субтитры)")])
            s.add(Subscription(user_id=9, scope="page", page_id=page.id, voice_filter=[56]))
            e10 = await s.scalar(select(Episode.id).where(Episode.page_id == page.id, Episode.episode == 10))
            e11 = await s.scalar(select(Episode.id).where(Episode.page_id == page.id, Episode.episode == 11))
            await svc.mark_voice_seen(s, e10, 56)       # вышла в озвучке подписки
            await svc.mark_voice_seen(s, e11, 238)      # вышла только в оригинале
            for ep, days in ((10, 0), (11, 1), (12, 2)):
                s.add(Schedule(page_id=page.id, season=1, episode=ep, air_date=TODAY + timedelta(days=days), aired=False))
            await s.commit()
            return [(r[1], r[2]) for r in
                    (await s.execute(CALENDAR_SQL, {"uid": 9, "days": CAL_DAYS, "back": CAL_LATE_DAYS})).all()]

    assert db(scenario) == [(1, 11), (1, 12)], "1×10 уже в дубляже — не ожидается; 1×11 только в оригинале — ждём"


def test_calendar_keeps_episode_that_aired_but_is_not_on_the_site(db):
    """12.09.2026: «неудачник» 1×12 вышел в эфир 11-го, на HDREZKA его ещё нет — и серия просто пропала
    из календаря. Теперь такие висят сверху отдельным блоком, а не исчезают."""
    from app.bot.main import CAL_DAYS, CAL_LATE_DAYS, CALENDAR_SQL, calendar_text

    async def scenario():
        async with session() as s:
            s.add(User(id=30))
            page = await _page(s, 962, "Неудачник", last=(1, 11), rows=[(1, 11)])
            await svc.subscribe_page(s, 30, page.id)
            s.add_all([Schedule(page_id=page.id, season=1, episode=12, air_date=TODAY - timedelta(days=1)),
                       Schedule(page_id=page.id, season=1, episode=13, air_date=TODAY + timedelta(days=6))])
            await s.commit()
            return (await s.execute(CALENDAR_SQL, {"uid": 30, "days": CAL_DAYS, "back": CAL_LATE_DAYS})).all()

    rows = db(scenario)
    assert [(r[1], r[2]) for r in rows] == [(1, 12), (1, 13)]
    text_ = calendar_text("ru", rows)
    assert text_.index("Неудачник — 1×12 · эфир") < text_.index("Ближайшие"), "просроченная — сверху"
    assert "Неудачник — 1×13" in text_


def test_episode_that_missed_the_updates_block_is_caught_when_the_page_is_reread(db):
    """Подстраховка: если серия не попала в блок обновлений, её видно по списку серий на самой странице.
    Перечитывание страниц с подписчиками (раз в 12 часов) записывает такую серию и уведомляет."""
    async def scenario():
        async with session() as s:
            s.add_all([User(id=31), User(id=32)])
            page = await _page(s, 963, "Сериал", last=(1, 11), rows=[(1, 11)],
                               voices=[(56, "Дубляж"), (238, "Оригинал (+субтитры)")])
            page.page_refreshed_at = svc.now() - timedelta(hours=13)
            await svc.subscribe_page(s, 31, page.id)                                  # любая озвучка
            s.add(Subscription(user_id=32, scope="page", page_id=page.id, voice_filter=[56]))
            await s.commit()
        site = FakeSite("", {963: title_page(963, "Сериал", 1, 12,
                                             [(56, "Дубляж", None), (238, "Оригинал (+субтитры)", None)])})
        async with session() as s:
            await Poller(site).refresh_pages(s)
            await s.commit()
        async with session() as s:                                # второй проход не дублирует
            await Poller(site).refresh_pages(s)
            await s.commit()
        return await _notifications(31), await _notifications(32)

    any_voice, dubbed = db(scenario)
    assert any_voice == [("episode", 1, 12)]
    assert dubbed == [("voice:56", 1, 12)], "список серий на странице — озвучки по умолчанию"


def test_my_subscriptions_show_episode_that_is_late(db):
    """В списке подписок у такой серии своя строка: «ждём 1×12, эфир 11 сен» вместо пустоты."""
    from app.bot.main import _render_my

    async def scenario():
        async with session() as s:
            s.add(User(id=33))
            page = await _page(s, 964, "Неудачник", last=(1, 11), rows=[(1, 11)])
            await svc.subscribe_page(s, 33, page.id)
            s.add(Schedule(page_id=page.id, season=1, episode=12, air_date=TODAY - timedelta(days=1)))
            await s.commit()
        return await _render_my(33)

    text_, _ = db(scenario)
    assert "1×11 · ждём 1×12, эфир" in text_


class FakeBot:
    """Подставной Telegram: запоминает отправленное. Постеров в тестовых страницах нет — уходит текст."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, [[b.text for b in row] for row in reply_markup.inline_keyboard]))
        return SimpleNamespace(message_id=1000 + len(self.sent), photo=None)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        raise AssertionError("в тестовых страницах постеров нет")


async def _two_new_episodes(user_id: int, digest_hour: int | None = None, stuck: bool = False) -> None:
    async with session() as s:
        s.add(User(id=user_id, quiet_from=None, quiet_to=None, digest_hour=digest_hour))
        page = await _page(s, 970 + user_id, f"Сериал {user_id}", last=(1, 2), rows=[(1, 1), (1, 2)])
        for eid in (await s.execute(select(Episode.id).where(Episode.page_id == page.id))).scalars():
            status, at = ("sending", "now() - interval '20 minutes'") if stuck else ("pending", "now()")
            await s.execute(text(f"INSERT INTO notifications (user_id, kind, ref_id, status, next_attempt_at) "
                                 f"VALUES (:u, 'episode', :e, '{status}', {at})"), {"u": user_id, "e": eid})
        await s.commit()


async def _send(monkeypatch_gap=True):
    from app import sender
    sender.PER_CHAT_GAP = 0
    bot = FakeBot()
    await sender.send_batch(bot, sender.RateLimiter(1000))
    async with session() as s:
        statuses = [tuple(r) for r in (await s.execute(text("SELECT status, tg_message_id FROM notifications ORDER BY id"))).all()]
    return bot.sent, statuses


def test_each_episode_is_its_own_post(db):
    """11.09.2026: две серии склеились в одно текстовое сообщение без постера — «уведомления не было»."""
    async def scenario():
        await _two_new_episodes(11)
        return await _send()

    sent, statuses = db(scenario)
    assert len(sent) == 2 and all(buttons == [["▶ Смотреть на HDrezka"]] for _, _, buttons in sent)
    assert statuses == [("sent", 1001), ("sent", 1002)], "у каждого уведомления номер своего сообщения Telegram"


def test_daily_digest_is_one_message(db):
    async def scenario():
        await _two_new_episodes(12, digest_hour=20)
        return await _send()

    sent, statuses = db(scenario)
    assert len(sent) == 1 and sent[0][1].startswith("🆕 <b>Вышли новые серии</b>")
    assert statuses == [("sent", 1001), ("sent", 1001)], "дайджест — одно сообщение на оба уведомления"


def test_stuck_sending_notification_is_retried(db):
    """Процесс упал между захватом и отправкой: уведомление не должно навсегда остаться «отправляется»."""
    async def scenario():
        await _two_new_episodes(13, stuck=True)
        return await _send()

    sent, statuses = db(scenario)
    assert len(sent) == 2 and [st for st, _ in statuses] == ["sent", "sent"]


class FailingBot(FakeBot):
    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        raise RuntimeError("Telegram недоступен")


def test_notification_is_never_abandoned(db):
    """До 11.09.2026 уведомление с пятью неудачными попытками больше не бралось в отправку — никогда."""
    async def scenario():
        await _two_new_episodes(14)
        async with session() as s:
            await s.execute(text("UPDATE notifications SET attempts = 7"))
            await s.commit()
        return await _send()

    sent, statuses = db(scenario)
    assert len(sent) == 2 and [st for st, _ in statuses] == ["sent", "sent"]


def test_transient_failure_is_retried_later_and_visible_in_health(db):
    from app import health, sender

    async def scenario():
        await _two_new_episodes(15)
        sender.PER_CHAT_GAP = 0
        await sender.send_batch(FailingBot(), sender.RateLimiter(1000))
        async with session() as s:
            rows = (await s.execute(text(
                "SELECT status, attempts, next_attempt_at > now() + interval '50 seconds' FROM notifications"))).all()
            quiet = await health.check(s, part="delivery")
            await s.execute(text("UPDATE notifications SET created_at = now() - interval '20 minutes'"))
            loud = await health.check(s, part="delivery")
        return rows, quiet, loud

    rows, quiet, loud = db(scenario)
    assert [tuple(r) for r in rows] == [("pending", 1, True), ("pending", 1, True)], "в очереди, повтор через минуту"
    assert quiet == [] and any("повторы после сбоя" in p for p in loud)


def test_health_sees_undelivered_and_skipped(db):
    from app import health

    async def scenario():
        await _two_new_episodes(16)
        async with session() as s:
            await s.execute(text("UPDATE notifications SET next_attempt_at = now() - interval '30 minutes'"))
            await svc.meta_set(s, "updates_failed", "2")
            problems = await health.check(s)
            await s.commit()
        return problems

    problems = db(scenario)
    assert any("не взято в отправку" in p for p in problems)
    assert any("не разобрано" in p for p in problems)


def test_poller_counts_skipped_events(db, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("кривая страница")

    async def scenario():
        monkeypatch.setattr(svc, "find_episode", boom)
        await _run(FakeSite(block({TODAY: [(990, "Сериал", "series", 1, 1, "Дубляж")]})))
        async with session() as s:
            return await svc.meta_get(s, "updates_failed")

    assert db(scenario) == "1"


def test_my_subscriptions_list(db):
    """11.09.2026: узкие кнопки «🎙 …» и «❌» не вмещали названий. Теперь по широкой кнопке на сериал, в тексте —
    серия, следующая и озвучка, без слова «франшиза»; сверху то, что выйдет раньше; до 15 на страницу поровну."""
    from app.bot.main import _render_my
    from app.i18n import fmt_date

    async def scenario():
        async with session() as s:
            s.add(User(id=20))
            soon = await _page(s, 1001, "Скоро", last=(1, 11), voices=[(56, "Дубляж")])
            later = await _page(s, 1002, "Позже", last=(2, 3))
            done = await _page(s, 1003, "Завершённый", last=(1, 12), finished=True)
            fr = Franchise(key_hdrezka_id=1004, name="Сага")
            s.add(fr)
            await s.flush()
            await _page(s, 1004, "Сага [ТВ-2]", last=(2, 5), franchise_id=fr.id)
            s.add_all([Schedule(page_id=soon.id, season=1, episode=12, air_date=TODAY + timedelta(days=1)),
                       Schedule(page_id=later.id, season=2, episode=4, air_date=TODAY + timedelta(days=5))])
            s.add(Subscription(user_id=20, scope="page", page_id=soon.id, voice_filter=[56]))
            await s.commit()
            small = await _render_my(20)
            await svc.subscribe_page(s, 20, later.id)
            await svc.subscribe_page(s, 20, done.id)
            await svc.subscribe_franchise(s, 20, fr.id)
            for i in range(14):
                extra = await _page(s, 1100 + i, f"Сериал {i}", last=(1, 1))
                await svc.subscribe_page(s, 20, extra.id)
            await s.commit()
        return small, await _render_my(20), await _render_my(20, page=1)

    (_, kb0), (text1, kb1), (text2, kb2) = db(scenario)
    assert len(kb0.inline_keyboard) == 1, "одна подписка — без переключателя страниц"
    soon_line = f"📺 <b>Скоро</b>\n1×11 · след. {fmt_date('ru', TODAY + timedelta(days=1))} · 🎙 Дубляж"
    assert text1.startswith("📋 <b>Ваши подписки (18)</b>\n\n" + soon_line)
    assert "📺 <b>Сага</b>\n2×5 · 🎙 любая" in text1 and "франшиз" not in text1
    buttons1 = [row[0].text for row in kb1.inline_keyboard]
    assert buttons1[:3] == ["📺 Скоро", "📺 Позже", "📺 Сага"] and len(buttons1) == 10, "18 записей — по 9"
    assert kb1.inline_keyboard[0][0].callback_data.endswith(":0")
    assert [b.text for b in kb1.inline_keyboard[-1]] == ["·", "1/2", "▶"]
    assert text2.rstrip().endswith("<i>Нажмите на сериал — там озвучка и отписка.</i>")
    assert "🔔 <b>Завершённый</b>\nзавершён, жду продолжения" in text2, "ждущие продолжения — в конце"
    assert [b.text for b in kb2.inline_keyboard[-1]] == ["◀", "2/2", "·"]


def test_health_sees_that_events_stopped(db):
    from app import health

    async def scenario():
        async with session() as s:
            before = await health.check(s)
            await svc.meta_set(s, "last_poll_ok", svc.now().isoformat())
            await svc.meta_set(s, "updates_ok_at", svc.now().isoformat())
            await _page(s, 900, "Живой", rows=[(1, 1)])
            after = await health.check(s)
            await s.commit()
        return before, after

    before, after = db(scenario)
    assert len(before) == 3 and after == []
