"""Конфигурация. Всё через env — ничего не хардкодим, домены и доступ меняются."""
import os
from dataclasses import dataclass, field


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(
        default_factory=lambda: [int(x) for x in _list("ADMIN_IDS", "")]
    )

    # --- База ---
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://rezka:rezka@postgres:5432/rezka"
    )

    # --- Доступ к сайту ---
    # Зеркала перебираются по порядку при 403/таймауте.
    base_urls: list[str] = field(
        default_factory=lambda: _list("HDREZKA_BASE_URLS", "https://rezka-ua.tv")
    )
    # SOCKS5 через SSH-туннель на сервер с незабаненным IP.
    proxy: str | None = os.getenv("HDREZKA_PROXY") or None
    impersonate: str = os.getenv("HDREZKA_IMPERSONATE", "chrome")
    user_agent: str = os.getenv(
        "HDREZKA_UA",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )

    # --- Вежливость к сайту ---
    request_delay: float = float(os.getenv("REQUEST_DELAY", "4.0"))   # сек между запросами
    request_jitter: float = float(os.getenv("REQUEST_JITTER", "2.0")) # случайная добавка
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "25"))

    # --- Поллер ---
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "180"))       # сек между обходами
    feed_sections: list[str] = field(
        default_factory=lambda: _list("FEED_SECTIONS", "series,animation")
    )
    feed_pages: int = int(os.getenv("FEED_PAGES", "1"))
    stale_alert_minutes: int = int(os.getenv("STALE_ALERT_MINUTES", "20"))

    # --- Рассылка ---
    send_rate: float = float(os.getenv("SEND_RATE", "25"))            # сообщений/сек
    send_batch: int = int(os.getenv("SEND_BATCH", "200"))


cfg = Config()
