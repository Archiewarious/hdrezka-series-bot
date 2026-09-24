"""Конфигурация. Всё через env — ничего не хардкодим, домены и доступ меняются."""
import os
from dataclasses import dataclass, field
from urllib.parse import quote


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _admin_ids() -> list[int]:
    """ADMIN_IDS — числовые id Telegram через запятую. Ошибка — понятным сообщением при старте, а не трейсбеком
    int() из недр конфигурации (24.09.2026)."""
    raw = _list("ADMIN_IDS", "")
    try:
        return [int(x) for x in raw]
    except ValueError:
        raise SystemExit(f"ADMIN_IDS: нужны числовые id Telegram через запятую, например 123456789,987654321; "
                         f"сейчас: {os.getenv('ADMIN_IDS')!r}") from None


def _database_url() -> str:
    """DATABASE_URL, а если не задан — собирается из POSTGRES_PASSWORD: пароль базы в .env в одном месте, а не в двух,
    которые расходились при смене (24.09.2026)."""
    if url := os.getenv("DATABASE_URL"):
        return url
    password = quote(os.getenv("POSTGRES_PASSWORD", "rezka"), safe="")
    return f"postgresql+asyncpg://rezka:{password}@postgres:5432/rezka"


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=_admin_ids)

    # --- База ---
    database_url: str = field(default_factory=_database_url)
    # Пул соединений на процесс (24.09.2026): у Postgres 100 соединений на всех, раньше каждый процесс брал до 30.
    # Бот — 10 + 10 (ответы людям параллельны), поллер и отправщик — 3 + 2 (docker-compose.yml).
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "10"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))

    # --- Доступ к сайту ---
    # Зеркала перебираются по порядку при 403/таймауте.
    base_urls: list[str] = field(
        default_factory=lambda: _list("HDREZKA_BASE_URLS", "https://rezka-ua.tv")
    )
    # SOCKS5 через SSH-туннель на сервер с незабаненным IP.
    proxy: str | None = os.getenv("HDREZKA_PROXY") or None
    impersonate: str = os.getenv("HDREZKA_IMPERSONATE", "chrome")
    # Пусто — User-Agent ставит curl_cffi, согласованный с impersonate: свой UA рядом с чужим TLS-отпечатком
    # выдаёт скрипт (24.09.2026). Задан явно — применяется.
    user_agent: str = os.getenv("HDREZKA_UA", "")
    # Домен для кнопок «Смотреть»: в базе адреса страниц хранятся путями (24.09.2026). По умолчанию —
    # первое зеркало из HDREZKA_BASE_URLS.
    public_url: str = field(default_factory=lambda: (
        os.getenv("HDREZKA_PUBLIC_URL") or _list("HDREZKA_BASE_URLS", "https://rezka-ua.tv")[0]).rstrip("/"))
    # Хосты постеров: качаем напрямую, без туннеля, поэтому только с CDN сайта (поддомены тоже). 24.09.2026
    poster_hosts: list[str] = field(default_factory=lambda: _list("POSTER_HOSTS", "hdrezka.ac"))
    # Anubis: выше этой сложности не решаем — сложность 7 это ~4 мин, 8 — час, 9 — 15 ч счёта в потоке.
    max_pow_difficulty: int = int(os.getenv("MAX_POW_DIFFICULTY", "6"))

    # --- Вежливость к сайту ---
    request_delay: float = float(os.getenv("REQUEST_DELAY", "4.0"))   # сек между запросами
    request_jitter: float = float(os.getenv("REQUEST_JITTER", "2.0")) # случайная добавка
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "25"))

    # --- Поллер ---
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "180"))       # сек между обходами
    feed_sections: list[str] = field(
        default_factory=lambda: _list("FEED_SECTIONS", "series,animation")
    )
    stale_alert_minutes: int = int(os.getenv("STALE_ALERT_MINUTES", "20"))
    # Внешний мониторинг: пинг после успешного цикла поллера и прохода отправщика, если здоровье чисто.
    # Пусто — выключено. В Telegram о сбоях не пишем никогда (решение владельца 1, 24.09.2026).
    healthcheck_ping_url: str = os.getenv("HEALTHCHECK_PING_URL", "")

    # --- Бот и сайт: общий потолок запросов бота к сайту на всех (24.09.2026) ---
    bot_site_per_minute: int = int(os.getenv("BOT_SITE_PER_MINUTE", "10"))
    bot_site_per_hour: int = int(os.getenv("BOT_SITE_PER_HOUR", "120"))

    # --- Рассылка ---
    send_rate: float = float(os.getenv("SEND_RATE", "25"))            # сообщений/сек
    send_batch: int = int(os.getenv("SEND_BATCH", "200"))


cfg = Config()
