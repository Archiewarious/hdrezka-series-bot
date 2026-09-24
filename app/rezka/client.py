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


class PageGone(Exception):
    """404/410: страницы нет на сайте. Касается одной страницы, а не доступа: без повторов и смены зеркала —
    иначе одна пропавшая страница стоила трёх запросов и выглядела как потеря доступа (24.09.2026)."""


def _solve_pow(random_data: str, difficulty: int, max_attempts: int | None = None) -> tuple[str, int] | None:
    """Anubis PoW: ищем nonce, при котором sha256(randomData + nonce) начинается с `difficulty` нулей.
    При difficulty 2 это ~256 хешей. Поток нельзя отменить, поэтому у перебора жёсткий предел:
    8 × 16^difficulty попыток при ожидаемых 16^difficulty (24.09.2026). Не нашли — None."""
    prefix = "0" * difficulty
    limit = max_attempts if max_attempts is not None else 8 * 16 ** difficulty
    for nonce in range(limit):
        digest = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if digest.startswith(prefix):
            return digest, nonce
    return None


def _parse_challenge(html: str) -> dict | None:
    """Задача Anubis со страницы: id, randomData до 256 символов, сложность — целое. Иначе None."""
    m = _CHALLENGE_RX.search(html)
    if not m:
        return None
    try:
        challenge = json.loads(m.group(1))["challenge"]
        difficulty = int(challenge.get("difficulty", 4))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    cid, data = challenge.get("id"), challenge.get("randomData")
    if not cid or not isinstance(data, str) or len(data) > 256 or difficulty < 0:
        return None
    return {"id": str(cid), "randomData": data, "difficulty": difficulty}


RETRY_AFTER_MAX = 120      # 429: ждём сколько просят, но не дольше двух минут


class RezkaClient:
    def __init__(self) -> None:
        self._session: requests.AsyncSession | None = None
        self._cookies: list = []           # cookie старой сессии, в т. ч. пропуск Anubis на 30 дней
        self._base_idx = 0
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return cfg.base_urls[self._base_idx].rstrip("/")

    def _next_mirror(self) -> None:
        self._base_idx = (self._base_idx + 1) % len(cfg.base_urls)
        log.warning("Переключаюсь на зеркало %s", self.base_url)

    def _make_session(self) -> requests.AsyncSession:
        """Новая HTTP-сессия. Отдельным методом — тесты подменяют его и в сеть не ходят."""
        proxies = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None
        headers = {
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if cfg.user_agent:
            headers["User-Agent"] = cfg.user_agent
        return requests.AsyncSession(impersonate=cfg.impersonate, proxies=proxies,
                                     timeout=cfg.request_timeout, headers=headers)

    async def _ensure_session(self) -> requests.AsyncSession:
        if self._session is None:
            self._session = self._make_session()
            for cookie in self._cookies:           # пропуск Anubis переживает пересоздание сессии
                self._session.cookies.jar.set_cookie(cookie)
        return self._session

    async def _reset_session(self) -> None:
        """Сеть/туннель отвалились: старую сессию закрываем (раньше она просто бросалась), cookie переносим."""
        old, self._session = self._session, None
        if old is None:
            return
        try:
            self._cookies = list(old.cookies.jar)
        except Exception:
            pass
        try:
            await old.close()
        except Exception as exc:
            log.debug("Старая сессия не закрылась: %s", exc)

    async def _throttle(self) -> None:
        """Не быстрее одного запроса в REQUEST_DELAY секунд, с джиттером.
        Рабочий IP один — его надо беречь."""
        wait = cfg.request_delay + random.uniform(0, cfg.request_jitter)
        elapsed = time.monotonic() - self._last_request
        if elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_request = time.monotonic()

    async def _solve_challenge(self, html: str, target: str) -> bool:
        """Проходит проверку Anubis. True — получилось. Сложность выше MAX_POW_DIFFICULTY — AccessBlocked сразу:
        перебор в потоке нельзя отменить, а блокировка клиента держится всё это время."""
        challenge = _parse_challenge(html)
        if challenge is None:
            log.error("Anubis: не разобрал задачу")
            return False
        difficulty = challenge["difficulty"]
        if difficulty > cfg.max_pow_difficulty:
            log.error("Anubis: сложность %s выше предела %s — не решаю", difficulty, cfg.max_pow_difficulty)
            raise AccessBlocked(f"Anubis: сложность {difficulty} выше предела {cfg.max_pow_difficulty}")
        log.info("Anubis: решаю PoW, difficulty=%s", difficulty)

        started = time.monotonic()
        # PoW считаем в потоке: при высокой difficulty он бы заблокировал event loop.
        solved = await asyncio.to_thread(_solve_pow, challenge["randomData"], difficulty)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if solved is None:
            log.error("Anubis: решение не найдено за предел попыток (difficulty=%s)", difficulty)
            return False
        digest, nonce = solved

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
                    await self._reset_session()
                    continue

                if resp.status_code == 403:
                    # Бан по IP — зеркала обычно не помогают, но попробуем следующее.
                    last_error = "403 (IP заблокирован)"
                    log.warning("403 от %s", self.base_url)
                    self._next_mirror()
                    continue

                if resp.status_code in (404, 410):
                    raise PageGone(f"{url}: HTTP {resp.status_code}")

                if resp.status_code == 429:
                    pause = _retry_after(resp)
                    last_error = f"HTTP 429, пауза {pause} с"
                    log.warning("429 от %s: жду %s с", self.base_url, pause)
                    await asyncio.sleep(pause)
                    continue

                if resp.status_code >= 500:
                    pause = min(5 * 2 ** attempt, 60)       # сайт лежит — не долбим, пауза растёт
                    last_error = f"HTTP {resp.status_code}"
                    log.warning("%s от %s: жду %s с", resp.status_code, self.base_url, pause)
                    await asyncio.sleep(pause)
                    continue

                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    continue

                if _CHALLENGE_RX.search(resp.text):
                    try:
                        if await self._solve_challenge(resp.text, url):
                            await self._throttle()
                            resp = await s.request(method, url, data=data, headers=headers,
                                                   allow_redirects=True)
                            if resp.status_code == 200 and not _CHALLENGE_RX.search(resp.text):
                                return resp.text
                        last_error = "не смог пройти проверку Anubis"
                    except AccessBlocked:
                        raise
                    except Exception as exc:     # сеть или формат ответа — внутри цикла повторов, не наружу
                        last_error = f"Anubis: {type(exc).__name__}: {exc}"
                        log.warning("Проверка Anubis упала (%s), попытка %s", last_error, attempt + 1)
                        await self._reset_session()
                    continue

                return resp.text

            raise AccessBlocked(f"{self.base_url}{path}: {last_error}")

    async def get(self, path: str, *, retries: int = 2) -> str:
        return await self.request("GET", path, retries=retries)

    async def home(self) -> str:
        """Главная: блок «Обновления» — неделя вышедших серий по дням, с озвучкой (F13)."""
        return await self.get("/")

    async def title_page(self, url: str) -> str:
        """Страница тайтла: озвучки, серии, франшиза, расписание."""
        return await self.get(url)

    async def search(self, query: str) -> str:
        from urllib.parse import quote_plus
        return await self.get(
            f"/search/?do=search&subaction=search&q={quote_plus(query)}"
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _retry_after(resp) -> int:
    """Retry-After в секундах, не больше RETRY_AFTER_MAX; нет или не число — 30 с."""
    try:
        return max(1, min(int(resp.headers.get("Retry-After", "30")), RETRY_AFTER_MAX))
    except (TypeError, ValueError, AttributeError):
        return 30
