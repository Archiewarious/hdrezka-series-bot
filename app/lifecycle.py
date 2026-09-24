"""Жизнь процессов поллера и отправщика (24.09.2026): остановка по SIGTERM, сторож зависаний, пинг внешнего
мониторинга, advisory lock «один экземпляр на базу».

В Telegram ничего не отправляется: о сбоях узнаёт внешний сервис, который ждёт пинга по HEALTHCHECK_PING_URL
(решение владельца 1). Пинг — только когда проверка здоровья чиста: адрес один на оба процесса, и живой процесс
иначе маскировал бы умерший.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time

import aiohttp
from sqlalchemy import text

from app.config import cfg
from app.db import engine

log = logging.getLogger("lifecycle")

# ----------------------------------------------------------------------------- остановка по SIGTERM

_stop = False      # флаг, а не asyncio.Event: событие привязывается к циклу, а тесты гоняют много циклов


def install_signal_handlers() -> None:
    """SIGTERM и SIGINT — мягкая остановка: цикл доделывает текущую единицу и выходит. Без обработчика процесс
    с PID 1 в контейнере игнорировал SIGTERM, и Docker через stop_grace_period убивал его SIGKILL посреди работы."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop)


def request_stop() -> None:
    global _stop
    if not _stop:
        log.info("Получен сигнал остановки — доделываю текущее и выхожу")
    _stop = True


def stopping() -> bool:
    return _stop


def reset() -> None:
    """Для тестов: новый процесс — новый флаг."""
    global _stop
    _stop = False


async def pause(seconds: float, beat: Watchdog | None = None) -> None:
    """Пауза, которую прерывает сигнал остановки (проверка раз в секунду); сторожа отмечаем по ходу, чтобы долгая
    пауза не сошла за зависание."""
    deadline = time.monotonic() + seconds
    while not _stop:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        if beat:
            beat.beat()
        await asyncio.sleep(min(left, 1.0))


# ----------------------------------------------------------------------------- сторож зависаний

class Watchdog:
    """Отдельный поток: если главный цикл не отмечался дольше порога, процесс завершается (os._exit(1)), и Docker
    перезапускает его. Healthcheck сам только помечает контейнер unhealthy — зависший процесс так и висел бы."""

    def __init__(self, name: str, limit_seconds: float, exit_fn=os._exit, check_every: float = 30.0) -> None:
        self.name, self.limit, self._exit, self._every = name, limit_seconds, exit_fn, check_every
        self._last = time.monotonic()
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def beat(self) -> None:
        self._last = time.monotonic()

    def start(self) -> Watchdog:
        self._thread = threading.Thread(target=self._run, name=f"watchdog-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()

    def _run(self) -> None:
        while not self._done.wait(self._every):
            idle = time.monotonic() - self._last
            if idle > self.limit:
                log.critical("%s: главный цикл не отмечался %.0f с (порог %.0f с) — завершаю процесс для перезапуска",
                             self.name, idle, self.limit)
                self._exit(1)
                return


# ----------------------------------------------------------------------------- пинг мониторинга

PING_EVERY = 60                                   # не чаще раза в минуту на процесс
_last_ping = 0.0


async def ping_if_healthy(s) -> bool:
    """После успешного цикла: пинг HEALTHCHECK_PING_URL, если проверка здоровья чиста. Напрямую, не через туннель,
    таймаут 5 с, ошибки игнорируются. Пустой адрес — выключено. True — пинг ушёл."""
    global _last_ping
    if not cfg.healthcheck_ping_url or time.monotonic() - _last_ping < PING_EVERY:
        return False
    from app import health                         # health тянет service; здесь — только по делу
    problems = await health.check(s)
    if problems:
        log.warning("Пинг мониторинга не отправлен — здоровье: %s", "; ".join(problems))
        _last_ping = time.monotonic()               # и лог не чаще раза в минуту
        return False
    _last_ping = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            async with http.get(cfg.healthcheck_ping_url) as resp:
                await resp.read()
        return True
    except Exception as exc:
        log.info("Пинг мониторинга не прошёл: %s", exc)
        return False


# ----------------------------------------------------------------------------- один экземпляр на базу

POLLER_LOCK = 0x52455A4B          # "REZK"
SENDER_LOCK = 0x53454E44          # "SEND"


async def acquire_lock(key: int, name: str, wait: bool = True):
    """Advisory lock «один экземпляр на базу». Соединение с локом — в autocommit: иначе оно висит «idle in
    transaction» и Postgres обрывает его по idle_in_transaction_session_timeout (23.09.2026). wait=False —
    None, если лок занят."""
    conn = await (await engine.connect()).execution_options(isolation_level="AUTOCOMMIT")
    while not await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
        if not wait:
            await conn.close()
            return None
        log.warning("Другой %s держит лок — жду 30 с", name)
        await asyncio.sleep(30)
    return conn


async def check_lock(conn) -> None:
    """Лок всё ещё наш. Соединение оборвалось (перезапуск базы, таймаут) — исключение: процесс выходит,
    Docker его перезапускает, и лок берётся заново."""
    held = await conn.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                                  " AND pid = pg_backend_pid()"))
    if not held:
        raise RuntimeError("Лок процесса потерян")
