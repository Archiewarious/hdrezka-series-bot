"""Отправщик: ответы Telegram (блокировка, flood control, отказ), очистка очереди, кэш постеров.
Telegram подменён, постеры не качаются — download подменяется."""
import io
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from PIL import Image
from sqlalchemy import select, text

from app import posters, sender
from app import service as svc
from app.db import session
from app.models import Episode, User
from test_integration import FakeBot, _page, _two_new_episodes


@pytest.fixture(autouse=True)
def no_gaps(monkeypatch):
    monkeypatch.setattr(sender, "PER_CHAT_GAP", 0)


class RaisingBot(FakeBot):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    async def send_message(self, *a, **kw):
        raise self.exc


async def _rows():
    async with session() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT status, error, next_attempt_at > now() + interval '2 seconds' FROM notifications ORDER BY id"))).all()]


def test_blocked_bot_deactivates_the_person(db):
    async def scenario():
        await _two_new_episodes(31)
        await sender.send_batch(RaisingBot(TelegramForbiddenError(method=None, message="bot was blocked by the user")),
                                sender.RateLimiter(1000))
        async with session() as s:
            active = await s.scalar(text("SELECT is_active FROM users WHERE id = 31"))
        return active, await _rows()

    active, rows = db(scenario)
    assert active is False and rows == [("failed", "bot blocked", False)] * 2


def test_flood_control_requeues_with_its_pause(db, monkeypatch):
    slept = []

    async def fake_pause(sec, beat=None):
        slept.append(sec)
    monkeypatch.setattr(sender.lifecycle, "pause", fake_pause)

    async def scenario():
        await _two_new_episodes(32)
        await sender.send_batch(RaisingBot(TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=30)),
                                sender.RateLimiter(1000))
        return await _rows()

    rows = db(scenario)
    assert slept == [30] and rows == [("pending", None, True)] * 2, "обе в очереди через 30 с, без ошибки"


def test_bad_request_fails_the_notification(db):
    async def scenario():
        await _two_new_episodes(33)
        await sender.send_batch(RaisingBot(TelegramBadRequest(method=None, message="chat not found")),
                                sender.RateLimiter(1000))
        return await _rows()

    rows = db(scenario)
    assert [(st, "chat not found" in (err or "")) for st, err, _ in rows] == [("failed", True)] * 2


def test_purge_removes_only_old_finished(db):
    async def scenario():
        async with session() as s:
            s.add(User(id=34))
            await s.flush()
            for n, (status, age) in enumerate([("sent", 8), ("sent", 1), ("failed", 8), ("pending", 30)]):
                await s.execute(text(
                    "INSERT INTO notifications (user_id, kind, ref_id, status, created_at, sent_at) VALUES "
                    "(34, 'episode', :r, CAST(:st AS varchar), now() - make_interval(days => :d), "
                    " CASE WHEN CAST(:st AS varchar) = 'sent' THEN now() - make_interval(days => :d) END)"),
                    {"r": n, "st": status, "d": age})
            await s.commit()
            removed = await sender.purge(s)
            await s.commit()
            left = sorted((await s.execute(text("SELECT status FROM notifications"))).scalars())
        return removed, left

    assert db(scenario) == (2, ["pending", "sent"])


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 141), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


class PhotoBot(FakeBot):
    """Telegram, который отклоняет старый file_id и принимает загрузку."""

    def __init__(self, reject_file_id: str | None = None):
        super().__init__()
        self.reject, self.photos = reject_file_id, []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        if isinstance(photo, str) and photo == self.reject:
            raise TelegramBadRequest(method=None, message="wrong file identifier")
        self.photos.append(photo if isinstance(photo, str) else "upload")
        return SimpleNamespace(message_id=500 + len(self.photos), photo=[SimpleNamespace(file_id="NEW-ID")])


async def _post_with_poster(user_id: int, hid: int, file_id: str | None):
    async with session() as s:
        s.add(User(id=user_id, quiet_from=None, quiet_to=None))
        page = await _page(s, hid, "С постером", last=(1, 2), rows=[(1, 2)])
        page.poster_url, page.poster_file_id = "https://static.hdrezka.ac/i/x.jpg", file_id
        await s.flush()
        eid = await s.scalar(select(Episode.id).where(Episode.page_id == page.id))
        await s.execute(text("INSERT INTO notifications (user_id, kind, ref_id) VALUES (:u, 'episode', :e)"),
                        {"u": user_id, "e": eid})
        await s.commit()
        return page.id


def test_rejected_file_id_is_reset_and_poster_uploaded_again(db, monkeypatch):
    async def fake_download(url):
        return _jpeg()
    monkeypatch.setattr(posters, "download", fake_download)

    async def scenario():
        pid = await _post_with_poster(35, 3500, "OLD-ID")
        bot = PhotoBot(reject_file_id="OLD-ID")
        await sender.send_batch(bot, sender.RateLimiter(1000))
        async with session() as s:
            file_id = await s.scalar(text("SELECT poster_file_id FROM pages WHERE id = :p"), {"p": pid})
        return bot.photos, bot.sent, file_id, (await _rows())[0][0]

    photos, texts, file_id, status = db(scenario)
    assert photos == ["upload"] and texts == [] and file_id == "NEW-ID" and status == "sent"


def test_poster_download_failure_falls_back_to_text(db, monkeypatch):
    async def no_download(url):
        return None
    monkeypatch.setattr(posters, "download", no_download)

    async def scenario():
        await _post_with_poster(36, 3600, None)
        bot = PhotoBot()
        await sender.send_batch(bot, sender.RateLimiter(1000))
        return bot.photos, len(bot.sent), (await _rows())[0][0]

    assert db(scenario) == ([], 1, "sent"), "постер не скачался — уведомление ушло текстом"


def test_new_part_is_rendered_with_public_link(db):
    async def scenario():
        async with session() as s:
            page = await _page(s, 3700, "Сага: фильм")
            page.content_type, page.url = "film", "/films/x/3700-f.html"
            await s.commit()
            r = await sender._render(s, 37, "new_part", page.id, "ru")
        return r.watch_url

    assert db(scenario) == svc.public_url("/films/x/3700-f.html")


def test_long_daily_digest_goes_out_in_parts_and_nothing_is_lost(db):
    """24.09.2026: текст дайджеста обрезался на 4000 символах, а все события помечались отправленными."""
    async def scenario():
        async with session() as s:
            s.add(User(id=38, quiet_from=None, quiet_to=None, digest_hour=20))
            for n in range(40):
                page = await _page(s, 3800 + n, f"Очень длинное название сериала номер {n} " + "ъ" * 60, last=(1, 1),
                                   rows=[(1, 1)])
                eid = await s.scalar(select(Episode.id).where(Episode.page_id == page.id))
                await s.execute(text("INSERT INTO notifications (user_id, kind, ref_id) VALUES (38, 'episode', :e)"),
                                {"e": eid})
            await s.commit()
        bot = FakeBot()
        await sender.send_batch(bot, sender.RateLimiter(1000))
        async with session() as s:
            statuses = (await s.execute(text("SELECT status, count(*) FROM notifications GROUP BY 1"))).all()
        return bot.sent, statuses

    sent, statuses = db(scenario)
    assert len(sent) > 1 and all(len(body) <= sender.TG_MAX_LEN for _, body, _ in sent)
    assert sum(body.count("Очень длинное название") for _, body, _ in sent) == 40, "все 40 событий в тексте"
    assert [tuple(r) for r in statuses] == [("sent", 40)]


def test_poster_cache_survives_errors_that_are_not_about_the_picture(db, monkeypatch):
    """Кэш постера сбрасывался при любом отказе Telegram — и картинка грузилась заново без причины."""
    class ChatGoneBot(PhotoBot):
        """Чат недоступен — и для фото, и для текста."""
        async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
            raise TelegramBadRequest(method=None, message="chat not found")

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            raise TelegramBadRequest(method=None, message="chat not found")

    async def scenario():
        pid = await _post_with_poster(39, 3900, "GOOD-ID")
        await sender.send_batch(ChatGoneBot(), sender.RateLimiter(1000))
        async with session() as s:
            file_id = await s.scalar(text("SELECT poster_file_id FROM pages WHERE id = :p"), {"p": pid})
        return file_id, (await _rows())[0][0]

    assert db(scenario) == ("GOOD-ID", "failed")


def test_unknown_refusal_of_the_picture_still_delivers_the_post_as_text(db, monkeypatch):
    """Telegram отказал фото по причине, которую мы не распознали: кэш постера не трогаем, пост уходит текстом.
    Раньше такое уведомление сразу помечалось неотправленным (25.09.2026)."""
    async def no_download(url):
        raise AssertionError("кэш верный — картинку заново не качаем")
    monkeypatch.setattr(posters, "download", no_download)

    class OddRefusalBot(PhotoBot):
        async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
            raise TelegramBadRequest(method=None, message="MEDIA_EMPTY")

    async def scenario():
        pid = await _post_with_poster(40, 4000, "GOOD-ID")
        bot = OddRefusalBot()
        await sender.send_batch(bot, sender.RateLimiter(1000))
        async with session() as s:
            file_id = await s.scalar(text("SELECT poster_file_id FROM pages WHERE id = :p"), {"p": pid})
        return file_id, len(bot.sent), (await _rows())[0][0]

    assert db(scenario) == ("GOOD-ID", 1, "sent")


def test_poster_only_from_allowed_hosts_and_formats():
    assert posters.safe_url("https://static.hdrezka.ac/i/x.jpg")
    assert not posters.safe_url("https://evil.example/x.jpg"), "чужой хост — не качаем"
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "GIF")
    with pytest.raises(Exception):
        posters._to_standard(buf.getvalue())


def test_stop_signal_interrupts_a_long_flood_control_pause(db):
    """Flood wait на 1000 с: сигнал остановки прерывает паузу, а взятые уведомления других людей сразу возвращаются
    в очередь. Раньше пауза была обычным sleep — остановка ждала её до SIGKILL (25.09.2026)."""
    import asyncio

    from app import lifecycle

    class FloodThenStop(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            lifecycle.request_stop()
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=1000)

    async def scenario():
        await _two_new_episodes(41)
        await _two_new_episodes(42)
        await asyncio.wait_for(sender.send_batch(FloodThenStop(), sender.RateLimiter(1000)), timeout=10)
        async with session() as s:
            return [tuple(r) for r in (await s.execute(text(
                "SELECT user_id, status, attempts, next_attempt_at > now() + interval '900 seconds' "
                "FROM notifications ORDER BY user_id, id"))).all()]

    assert db(scenario) == [(41, "pending", 1, True)] * 2 + [(42, "pending", 0, False)] * 2, \
        "первому — повтор после flood wait, второму — сразу в очередь без штрафа"
