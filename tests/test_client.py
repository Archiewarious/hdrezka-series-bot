"""Клиент сайта на подменённой сессии: ни одного запроса в сеть."""
import asyncio
import dataclasses
import http.cookiejar
import json
import time
from types import SimpleNamespace

import pytest

import app.rezka.client as client_mod
from app.config import cfg
from app.rezka.client import AccessBlocked, PageGone, RezkaClient, _solve_pow


class FakeResp:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class Script:
    """Ответы сайта по порядку — общие для всех сессий клиента: после сброса новая сессия тоже подменная."""

    def __init__(self, responses):
        self.responses, self.calls, self.sessions = list(responses), [], []

    @property
    def closed(self) -> bool:
        return bool(self.sessions) and self.sessions[0].closed


class FakeSession:
    def __init__(self, script: Script):
        self.script, self.closed = script, False
        self.cookies = SimpleNamespace(jar=http.cookiejar.CookieJar())
        self.headers = {}
        script.sessions.append(self)

    async def request(self, method, url, **kw):
        self.script.calls.append(url)
        r = self.script.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def close(self):
        self.closed = True


def client_with(responses) -> tuple[RezkaClient, Script]:
    c = RezkaClient()
    script = Script(responses)
    c._make_session = lambda: FakeSession(script)

    async def no_pause():
        pass
    c._throttle = no_pause
    return c, script


@pytest.mark.parametrize("status", [404, 410])
def test_gone_page_is_not_retried_and_mirror_stays(status):
    """24.09.2026: 404 шёл как любая ошибка — три попытки и потом AccessBlocked, будто потерян доступ."""
    c, script = client_with([FakeResp(status)])
    with pytest.raises(PageGone):
        asyncio.run(c.get("/series/x/1-a.html"))
    assert len(script.calls) == 1 and c._base_idx == 0


def test_ok_page_is_returned():
    c, script = client_with([FakeResp(200, "<html>ok</html>")])
    assert asyncio.run(c.get("/x/1-a.html")) == "<html>ok</html>"


@pytest.fixture
def mirrors(monkeypatch):
    monkeypatch.setattr(client_mod, "cfg", dataclasses.replace(cfg, base_urls=["https://m1.test", "https://m2.test"]))


@pytest.fixture
def sleeps(monkeypatch):
    """asyncio.sleep в клиенте — без ожидания, паузы записываются."""
    pauses = []

    async def fake_sleep(sec):
        pauses.append(sec)
    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    return pauses


def test_title_page_is_read_from_the_next_mirror(mirrors):
    """24.09.2026: страница хранилась абсолютным адресом, и после 403 клиент снова шёл на тот же домен."""
    c, script = client_with([FakeResp(403), FakeResp(200, "ok")])
    assert asyncio.run(c.title_page("/animation/x/88552-a.html")) == "ok"
    assert script.calls == ["https://m1.test/animation/x/88552-a.html", "https://m2.test/animation/x/88552-a.html"]


def test_429_waits_retry_after_but_not_longer_than_two_minutes(sleeps):
    c, script = client_with([FakeResp(429, headers={"Retry-After": "7"}), FakeResp(429, headers={"Retry-After": "9999"}),
                           FakeResp(200, "ok")])
    assert asyncio.run(c.get("/x/1-a.html")) == "ok"
    assert sleeps == [7, 120]


def test_5xx_pause_grows(sleeps):
    c, script = client_with([FakeResp(502), FakeResp(503), FakeResp(500)])
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))
    assert sleeps == [5, 10, 20] and len(script.calls) == 3


def test_network_error_closes_the_old_session_and_keeps_cookies():
    """Сессия бросалась без закрытия, а с ней и пропуск Anubis на 30 дней."""
    c, script = client_with([ConnectionError("туннель"), ConnectionError("туннель"), ConnectionError("туннель")])

    async def scenario():
        first = await c._ensure_session()
        first.cookies.jar.set_cookie(http.cookiejar.Cookie(
            0, "techaro.lol-anubis-auth", "jwt", None, False, "m1.test", True, False, "/", True, True, None, False,
            None, None, {}))
        with pytest.raises(AccessBlocked):
            await c.get("/x/1-a.html")
        fresh = await c._ensure_session()
        return first, fresh, [ck.name for ck in fresh.cookies.jar]

    first, fresh, names = asyncio.run(scenario())
    assert first.closed and fresh is not first and names == ["techaro.lol-anubis-auth"]


def _challenge_page(difficulty: int, **extra) -> str:
    body = {"challenge": {"id": "abc", "randomData": "r" * 64, "difficulty": difficulty, **extra}}
    return f'<html><script id="anubis_challenge" type="application/json">{json.dumps(body)}</script></html>'


def test_anubis_easy_task_is_solved():
    digest, nonce = _solve_pow("r" * 64, 1)
    assert digest.startswith("0")
    c, script = client_with([FakeResp(200, _challenge_page(1)), FakeResp(200, "pass"), FakeResp(200, "контент")])
    assert asyncio.run(c.get("/x/1-a.html")) == "контент"
    assert "pass-challenge" in script.calls[1]


def test_anubis_too_hard_task_fails_fast():
    """Сложность 99 крутилась бы в потоке вечно, держа блокировку клиента."""
    c, script = client_with([FakeResp(200, _challenge_page(99))])
    t0 = time.monotonic()
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))
    assert time.monotonic() - t0 < 1 and len(script.calls) == 1, "без решения и без запроса «пройдено»"


def test_pow_has_a_hard_attempt_limit():
    assert _solve_pow("r", 64, max_attempts=1000) is None


@pytest.mark.parametrize("bad", [
    '<script id="anubis_challenge">не json</script>',
    _challenge_page(1, id=""),
    _challenge_page(1, randomData="x" * 300),
    '<script id="anubis_challenge">{"challenge": {"id": "a", "randomData": "r", "difficulty": "много"}}</script>',
])
def test_broken_challenge_is_access_blocked_not_a_crash(bad):
    c, script = client_with([FakeResp(200, bad)] * 3)
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))


def test_network_error_inside_anubis_becomes_access_blocked():
    """Сетевое исключение в _solve_challenge выходило наружу как есть и роняло цикл, а не AccessBlocked."""
    challenge = FakeResp(200, _challenge_page(1))
    c, script = client_with([challenge, ConnectionError("обрыв"), challenge, ConnectionError("обрыв"),
                           challenge, ConnectionError("обрыв")])
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))
    assert script.closed


def test_no_user_agent_by_default():
    """Свой UA рядом с чужим TLS-отпечатком выдаёт скрипт: по умолчанию его ставит curl_cffi.
    Настоящая сессия только создаётся — запросов нет (no_network в conftest)."""
    async def scenario():
        c = RezkaClient()
        sess = c._make_session()
        ua = sess.headers.get("User-Agent")
        await sess.close()
        return ua
    assert cfg.user_agent == "" and asyncio.run(scenario()) is None


def test_403_on_every_mirror_is_access_blocked(mirrors):
    c, script = client_with([FakeResp(403), FakeResp(403), FakeResp(403)])
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))
    assert [u.split("/x/")[0] for u in script.calls] == ["https://m1.test", "https://m2.test", "https://m1.test"]


def test_failed_anubis_pass_is_retried_then_blocked():
    challenge = FakeResp(200, _challenge_page(1))
    c, script = client_with([challenge, FakeResp(200, "pass"), FakeResp(200, _challenge_page(1))] * 3)
    with pytest.raises(AccessBlocked):
        asyncio.run(c.get("/x/1-a.html"))
    assert len(script.calls) == 9, "каждая попытка: задача, «пройдено», снова задача"
