"""HTTP-клиент к HDREZKA.

Единственное место, где живёт вся боль с доступом:
  * SOCKS5-прокси (SSH-туннель на сервер с незабаненным IP);
  * TLS-фингерпринт браузера (curl_cffi impersonate);
  * proof-of-work Anubis;
  * перебор зеркал;
  * вежливые паузы между запросами.

Всё остальное приложение об этом не знает и работает с готовым HTML.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time

from curl_cffi import requests

from app.config import cfg

log = logging.getLogger(__name__)

_CHALLENGE_RX = re.compile(
    r'<script id="anubis_challenge"[^>]*>(.*?)</script>', re.S
)
_ANUBIS_PASS = "/.within.website/x/cmd/anubis/api/pass-challenge"


class AccessBlocked(Exception):
    """Ни одно зеркало не отдало контент — IP забанен или сайт лежит."""


def _solve_pow(random_data: str, difficulty: int) -> tuple[str, int]:
    """Anubis PoW: ищем nonce, при котором sha256(randomData + nonce)
    начинается с `difficulty` нулей. При difficulty 2 это ~256 хешей."""
    prefix = "0" * difficulty
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if digest.startswith(prefix):
            return digest, nonce
        nonce += 1


class RezkaClient:
    def __init__(self) -> None:
        self._session: requests.AsyncSession | None = None
        self._base_idx = 0
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return cfg.base_urls[self._base_idx].rstrip("/")

    def _next_mirror(self) -> None:
        self._base_idx = (self._base_idx + 1) % len(cfg.base_urls)
        log.warning("Переключаюсь на зеркало %s", self.base_url)

    async def _ensure_session(self) -> requests.AsyncSession:
        if self._session is None:
            proxies = (
                {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None
            )
            self._session = requests.AsyncSession(
                impersonate=cfg.impersonate,
                proxies=proxies,
                timeout=cfg.request_timeout,
                headers={
                    "User-Agent": cfg.user_agent,
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        return self._session

    async def _throttle(self) -> None:
        """Не быстрее одного запроса в REQUEST_DELAY секунд, с джиттером.
        Рабочий IP один — его надо беречь."""
        wait = cfg.request_delay + random.uniform(0, cfg.request_jitter)
        elapsed = time.monotonic() - self._last_request
        if elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_request = time.monotonic()

    async def _solve_challenge(self, html: str, target: str) -> bool:
        """Проходит проверку Anubis. Возвращает True, если получилось."""
        m = _CHALLENGE_RX.search(html)
        if not m:
            return False
        try:
            challenge = json.loads(m.group(1))["challenge"]
        except (ValueError, KeyError):
            log.error("Anubis: не разобрал челлендж")
            return False

        difficulty = int(challenge.get("difficulty", 4))
        log.info("Anubis: решаю PoW, difficulty=%s", difficulty)

        started = time.monotonic()
        # PoW считаем в потоке: при высокой difficulty он бы заблокировал event loop.
        digest, nonce = await asyncio.to_thread(
            _solve_pow, challenge["randomData"], difficulty
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        s = await self._ensure_session()
        resp = await s.get(
            f"{self.base_url}{_ANUBIS_PASS}",
            params={
                "id": challenge["id"],
                "response": digest,
                "nonce": nonce,
                "redir": target,
                "elapsedTime": elapsed_ms,
            },
            allow_redirects=True,
        )
        ok = resp.status_code == 200 and not _CHALLENGE_RX.search(resp.text)
        log.info(
            "Anubis: %s (nonce=%s, %s мс)",
            "пройдено" if ok else "НЕ пройдено", nonce, elapsed_ms,
        )
        return ok

    async def request(self, method: str, path: str, *, data: dict | None = None,
                      headers: dict | None = None, retries: int = 2) -> str:
        """Возвращает тело ответа. path — абсолютный URL или путь от корня зеркала.
        Всё, что связано с доступом (прокси, Anubis, зеркала, паузы), — здесь."""
        async with self._lock:
            last_error = "неизвестно"

            for attempt in range(retries + 1):
                url = path if path.startswith("http") else f"{self.base_url}{path}"
                await self._throttle()

                s = await self._ensure_session()
                try:
                    resp = await s.request(method, url, data=data, headers=headers,
                                           allow_redirects=True)
                except Exception as exc:  # сеть/туннель отвалился
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("Запрос упал (%s), попытка %s", last_error, attempt + 1)
                    self._session = None
                    continue

                if resp.status_code == 403:
                    # Бан по IP — зеркала обычно не помогают, но попробуем следующее.
                    last_error = "403 (IP заблокирован)"
                    log.warning("403 от %s", self.base_url)
                    self._next_mirror()
                    continue

                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    continue

                if _CHALLENGE_RX.search(resp.text):
                    if await self._solve_challenge(resp.text, url):
                        await self._throttle()
                        resp = await s.request(method, url, data=data, headers=headers,
                                               allow_redirects=True)
                        if resp.status_code == 200 and not _CHALLENGE_RX.search(resp.text):
                            return resp.text
                    last_error = "не смог пройти проверку Anubis"
                    continue

                return resp.text

            raise AccessBlocked(f"{self.base_url}{path}: {last_error}")

    async def get(self, path: str, *, retries: int = 2) -> str:
        return await self.request("GET", path, retries=retries)

    async def feed(self, section: str, page: int = 1) -> str:
        """Лента обновлений раздела: сериалы, аниме и т.д."""
        suffix = "" if page == 1 else f"page/{page}/"
        return await self.get(f"/{section}/{suffix}?filter=last")

    async def title_page(self, url: str) -> str:
        """Страница тайтла: озвучки, серии, франшиза, расписание."""
        return await self.get(url)

    async def episodes_html(self, hdrezka_id: int, translator_id: int) -> str:
        """Список серий конкретной озвучки — тот же ajax, что дёргает плеер сайта.
        Возвращает HTML-фрагмент с .b-simple_episode__item (см. parser.parse_episodes_html)."""
        import json, time
        body = await self.request(
            "POST", f"/ajax/get_cdn_series/?t={int(time.time() * 1000)}",
            data={"id": str(hdrezka_id), "translator_id": str(translator_id), "action": "get_episodes"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.base_url}/"},
        )
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise AccessBlocked(f"ajax get_cdn_series: не JSON ({body[:60]!r})") from exc
        if not payload.get("success", False):
            raise AccessBlocked(f"ajax get_cdn_series: success={payload.get('success')}")
        return payload.get("episodes", "")

    async def search(self, query: str) -> str:
        from urllib.parse import quote_plus
        return await self.get(
            f"/search/?do=search&subaction=search&q={quote_plus(query)}"
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
