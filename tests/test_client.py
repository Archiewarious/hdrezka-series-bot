"""Клиент сайта на подменённой сессии: ни одного запроса в сеть."""
import asyncio

import pytest

from app.rezka.client import PageGone, RezkaClient


class FakeResp:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text, self.headers = status, text, {}


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.closed = list(responses), [], False

    async def request(self, method, url, **kw):
        self.calls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def close(self):
        self.closed = True


def client_with(responses) -> tuple[RezkaClient, FakeSession]:
    c = RezkaClient()
    fake = FakeSession(responses)
    c._session = fake

    async def no_pause():
        pass
    c._throttle = no_pause
    return c, fake


@pytest.mark.parametrize("status", [404, 410])
def test_gone_page_is_not_retried_and_mirror_stays(status):
    """24.09.2026: 404 шёл как любая ошибка — три попытки и потом AccessBlocked, будто потерян доступ."""
    c, fake = client_with([FakeResp(status)])
    with pytest.raises(PageGone):
        asyncio.run(c.get("/series/x/1-a.html"))
    assert len(fake.calls) == 1 and c._base_idx == 0


def test_ok_page_is_returned():
    c, fake = client_with([FakeResp(200, "<html>ok</html>")])
    assert asyncio.run(c.get("/x/1-a.html")) == "<html>ok</html>"
