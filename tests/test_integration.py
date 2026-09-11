"""Путь события от блока обновлений до уведомления — на настоящем Postgres.

Каждый тест — живой случай 10.09.2026 или защита от него. Сайт подменён: главная и страницы тайтлов
собираются здесь же по той вёрстке, которую разбирает app/rezka/parser.py.
"""
import re
from datetime import date, timedelta

from sqlalchemy import select, text

from app import service as svc
from app.db import session
from app.models import Episode, Franchise, Page, Subscription, User, Voice
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
    from app.bot.main import CAL_DAYS, CALENDAR_SQL

    async def scenario():
        async with session() as s:
            return (await s.execute(CALENDAR_SQL, {"uid": 1, "days": CAL_DAYS})).all()

    assert db(scenario) == []


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
