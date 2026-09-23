"""Аудит защиты 23.09.2026: неправильный ввод, огромные числа, разметка из чужих строк, флуд, «бомбы».

callback_data присылает клиент, и нестандартный клиент пришлёт что угодно — поэтому каждая кнопка
принимается только целиком по своему шаблону (app/bot/main.py, CB), а база держит границы значений сама.
"""
import asyncio
import io
import pathlib
import re
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import text

from app import posters
from app.bot import guard
from app.bot.main import CB_RX, FloodGuard, _PATH_RX, _clip, calendar_text, toggle_voice

ROOT = pathlib.Path(__file__).parents[1]


def _matches(data: str) -> list[str]:
    return [name for name, rx in CB_RX.items() if rx.match(data)]


# Кнопки, которые бот создаёт сам: каждая — ровно под одним шаблоном.
LEGIT = ["startlang:ru", "startlang:uk", "startlang:en", "site:0123456789ab", "sub:90932", "wait:90932",
         "subf:12", "subf:12:183", "subf_all:12", "sched:p:183", "sched:f:12", "pcard:183", "fcard:12",
         "voices:15", "vt:15:56", "vany:15", "card:15", "card:15:0", "card:15:3", "my:0", "my:1", "unsub:15",
         "unsubq:15", "keep:15", "set:photos:1", "set:photos:0", "set:photos", "set:quiet:1", "set:quiet:0",
         "set:digest:0", "set:digest:1", "set:tz:-1", "set:tz:1", "set:quietcfg", "set:voice", "set:lang",
         "set:back", "setq:off", "setq:f:1", "setq:f:-1", "setq:t:1", "setq:t:-1", "setl:ru", "setl:en",
         "setv:any", "setv:56", "fb", "fb:bug", "fb:idea", "fb:collab", "fb:other", "fb:cancel", "noop"]

HOSTILE = ["pcard:9999999999", "pcard:99999999999999999999", "pcard:-1", "pcard:1 ", "pcard:1\n", "pcard:١",
           "pcard:", "pcard:1:2", "vt:1:abc", "vt:1", "vt:1:2:3", "set:tz:100", "set:tz:-12", "set:photos:2",
           "setq:f:1000", "setq:f", "setq:x:1", "my:99999", "my:-1", "card:1:99999", "sched:x:1", "sched:p",
           "startlang:de", "setl:xx", "setv:1e9", "setv:-5", "fb:hack", "unsub:1;DROP TABLE users", "",
           "a" * 64, "sub:12abc", "wait:", "site:xyz", "site:0123456789abcdef", "noop2", "fb:"]


@pytest.mark.parametrize("data", LEGIT)
def test_every_real_button_passes_exactly_one_filter(data):
    assert len(_matches(data)) == 1, (data, _matches(data))


@pytest.mark.parametrize("data", HOSTILE)
def test_forged_button_passes_no_filter(data):
    assert _matches(data) == [], f"{data!r} → {_matches(data)}: дойдёт до int() или до базы"


def test_every_button_prefix_in_the_code_has_a_filter():
    """Новая кнопка без шаблона молча перестанет работать — ловим это здесь, а не в проде."""
    src = (ROOT / "app/bot/main.py").read_text()
    prefixes = set(re.findall(r'f?"([a-z_]+):[^"]*"', src)) - {"https", "http", "tg"}
    samples = {pre: next((d for d in LEGIT if d.startswith(pre + ":")), None) for pre in prefixes}
    assert {pre for pre, d in samples.items() if d is None} == set(), "у кнопки нет примера в LEGIT и шаблона"


def test_link_regex_is_bounded():
    """220 мс на 4096 «/» — весь бот стоит. Ищем только в начале сообщения и только до 9 цифр."""
    t0 = time.perf_counter()
    for _ in range(10):
        _PATH_RX.search(("/" * 4096)[:guard.MAX_LINK_SCAN])
    assert (time.perf_counter() - t0) / 10 < 0.02
    assert _PATH_RX.search("https://rezka-ua.tv/animation/fantasy/90932-velikiy.html").group(2) == "90932"
    assert _PATH_RX.search("/animation/x/12345678901-a.html") is None, "10+ цифр — не id, в int4 не влезет"
    assert _PATH_RX.search("/animation/x/١٢٣-a.html") is None, "только ASCII-цифры"


def test_toggle_voice_accepts_only_real_dubs_and_caps_the_list():
    assert toggle_voice(None, 56, {56, 238}) == [56]
    assert toggle_voice([56], 999999, {56, 238}) == [56], "чужой id не добавляется"
    assert toggle_voice([56, 777], 777, {56}) == [56], "выбранную снять можно всегда, даже исчезнувшую"
    assert toggle_voice([56], 56, {56}) is None, "сняли последнюю — снова «любая»"
    many = list(range(1, guard.MAX_VOICES + 1))
    assert toggle_voice(many, 5000, set(range(1, 6000))) == many, "больше MAX_VOICES не набрать"


def test_clip_keeps_messages_under_the_telegram_limit():
    long = "\n".join(f"  • <b>Сериал {i}</b> — 1×{i}" for i in range(500))
    out = _clip(long)
    assert len(out) <= guard.TEXT_MAX and out.endswith("\n…")
    assert out.count("<b>") == out.count("</b>"), "режем по границе строки — теги не рвутся"
    assert _clip("коротко") == "коротко"


def test_calendar_escapes_titles():
    from datetime import date, timedelta
    rows = [("Tom & <Jerry>", 1, 2, date.today() + timedelta(days=1))]
    out = calendar_text("ru", rows)
    assert "Tom &amp; &lt;Jerry&gt;" in out and "<Jerry>" not in out


def test_flood_guard_drops_excess_updates_silently(monkeypatch):
    import app.bot.main as main
    monkeypatch.setattr(guard, "flood", guard.UserLimiter(per_minute=5, per_hour=100))
    monkeypatch.setattr(main, "cfg", SimpleNamespace(admin_ids=[777]))
    calls = []

    async def handler(event, data):
        calls.append(1)
        return "ok"

    def update(uid):
        return SimpleNamespace(message=SimpleNamespace(from_user=SimpleNamespace(id=uid)), callback_query=None)

    async def run():
        mw = FloodGuard()
        results = [await mw(handler, update(424242), {}) for _ in range(8)]
        admin = [await mw(handler, update(777), {}) for _ in range(8)]
        return results, admin

    results, admin = asyncio.run(run())
    assert results == ["ok"] * 5 + [None] * 3
    assert all(r == "ok" for r in admin), "автору отвечать на обращения не мешаем"


@pytest.mark.parametrize("url,ok", [
    ("https://static.hdrezka.ac/i/2026/8/9/x.jpg", True),
    ("http://static.hdrezka.ac/i/x.jpg", False),
    ("https://169.254.169.254/latest/meta-data", False),
    ("https://127.0.0.1/x.jpg", False),
    ("https://[::1]/x.jpg", False),
    ("https://localhost/x.jpg", False),
    ("file:///etc/passwd", False),
    ("", False),
    (None, False),
])
def test_poster_url_must_be_https_domain(url, ok):
    assert posters.safe_url(url) is ok


def test_poster_download_refuses_internal_address_without_network():
    assert asyncio.run(posters.download("http://169.254.169.254/latest/meta-data")) is None


def test_decompression_bomb_is_rejected_not_sent():
    """9000×9000 из одних нулей — 10 КБ файла и 240 МБ в памяти после распаковки."""
    buf = io.BytesIO()
    Image.new("1", (9000, 9000)).save(buf, "PNG")
    with pytest.raises(posters.PosterRejected):
        posters._to_standard(buf.getvalue())
    assert asyncio.run(posters.to_standard(buf.getvalue())) is None, "такую не отправляем и в исходном виде"


# ----------------------------------------------------------------------------- с базой

def test_titles_from_the_site_are_escaped_in_cards(db):
    from app.bot.main import _render_franchise_card, _render_franchise_overview, _render_page_card
    from app.db import session
    from app.models import Franchise, Page, User
    from app import service as svc

    async def scenario():
        async with session() as s:
            s.add(User(id=40))
            fr = Franchise(key_hdrezka_id=4000, name="Сага <i>&")
            s.add(fr)
            await s.flush()
            page = Page(hdrezka_id=4000, title="Tom & <Jerry>", url="https://rezka.test/series/y/4000-p.html",
                        content_type="series", last_season=1, last_episode=2, franchise_id=fr.id,
                        page_refreshed_at=svc.now())
            s.add(page)
            await s.flush()
            await svc.subscribe_franchise(s, 40, fr.id)
            await s.commit()
            return (await _render_page_card(40, page.id))[0], (await _render_franchise_card(40, fr.id))[0], \
                (await _render_franchise_overview(40, fr.id, None))[0]

    for out in db(scenario):
        assert "<Jerry>" not in out and "<i>&" not in out, out
        assert "&lt;Jerry&gt;" in out or "Сага &lt;i&gt;&amp;" in out, out


def test_missing_page_and_franchise_answer_instead_of_crashing(db):
    from app.bot.main import _render_franchise_card, _render_page_card
    from app.db import session
    from app.models import User

    async def scenario():
        async with session() as s:
            s.add(User(id=41))
            await s.commit()
        return (await _render_page_card(41, 999999))[0], (await _render_franchise_card(41, 999999))[0]

    page, fr = db(scenario)
    assert page == "Карточка устарела — повторите поиск." and fr == "Франшиза не найдена."


@pytest.mark.parametrize("sql", [
    "UPDATE users SET tz_offset = 100 WHERE id = 42",
    "UPDATE users SET quiet_from = 24 WHERE id = 42",
    "UPDATE users SET digest_hour = -1 WHERE id = 42",
    "UPDATE users SET lang = 'xx' WHERE id = 42",
    "UPDATE users SET default_voice_filter = ARRAY(SELECT generate_series(1, 31)) WHERE id = 42",
])
def test_database_holds_value_ranges_itself(db, sql):
    from sqlalchemy.exc import IntegrityError
    from app.db import session
    from app.models import User

    async def scenario():
        async with session() as s:
            s.add(User(id=42))
            await s.commit()
        async with session() as s:
            with pytest.raises(IntegrityError):
                await s.execute(text(sql))
                await s.commit()

    db(scenario)


def test_hung_queries_are_cut_off(db):
    from app.db import session

    async def scenario():
        async with session() as s:
            return (await s.scalar(text("SHOW statement_timeout")),
                    await s.scalar(text("SHOW idle_in_transaction_session_timeout")))

    assert db(scenario) == ("1min", "10min")


def test_poller_lock_connection_is_not_idle_in_transaction(db):
    """23.09.2026: соединение с локом поллера висело «idle in transaction» — таймаут простоя в транзакции
    оборвал бы его через 10 минут, и лок «один поллер на базу» молча пропал бы."""
    from app.db import session
    from app.poller import acquire_lock, check_lock

    async def scenario():
        conn = await acquire_lock()
        try:
            await check_lock(conn)
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            async with session() as s:
                state = await s.scalar(text("SELECT state FROM pg_stat_activity WHERE pid = :p"), {"p": pid})
        finally:
            await conn.close()
        return state

    assert db(scenario) == "idle"


def test_lost_poller_lock_is_detected(db):
    from app.poller import acquire_lock, check_lock

    async def scenario():
        conn = await acquire_lock()
        try:
            await conn.execute(text("SELECT pg_advisory_unlock_all()"))
            with pytest.raises(RuntimeError):
                await check_lock(conn)
        finally:
            await conn.close()

    db(scenario)
