"""Здоровье потока событий — одна проверка для /stats и для healthcheck контейнера поллера.

Сбой 05–10.09.2026 длился пять дней, потому что здоровьем считалось «цикл завершился»: поллер
отмечался каждые три минуты и при этом не видел серий. Здесь проверяется, что события идут.

    python -m app.health     # код выхода 1 и причины построчно, если что-то не так
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

from app import service as svc
from app.config import cfg

POLL_STALE = timedelta(minutes=cfg.stale_alert_minutes)
EVENTS_STALE = timedelta(minutes=cfg.stale_alert_minutes)
NO_NEW_EPISODES = timedelta(hours=12)    # на сайте 70–100 событий в сутки (F13): полсуток без новых серий — сбой


def _fmt(d: timedelta) -> str:
    hours = int(d.total_seconds() // 3600)
    return f"{hours} ч" if hours else f"{int(d.total_seconds() // 60)} мин"


async def check(s, at: datetime | None = None) -> list[str]:
    at = at or svc.now()
    problems: list[str] = []
    for key, limit, what in (("last_poll_ok", POLL_STALE, "поллер не завершал цикл"),
                             ("updates_ok_at", EVENTS_STALE, "блок обновлений не разбирается")):
        value = await svc.meta_get(s, key)
        if not value:
            problems.append(f"{what}: ни разу")
        elif at - datetime.fromisoformat(value) > limit:
            problems.append(f"{what} уже {_fmt(at - datetime.fromisoformat(value))}")
    last_new = await s.scalar(text("SELECT max(first_seen_at) FROM episodes WHERE first_seen_at > CAST(:after AS timestamptz)"),
                              {"after": svc.SEED_BEFORE})
    if last_new is None:
        problems.append("новых серий не было ни разу")
    elif at - last_new > NO_NEW_EPISODES:
        problems.append(f"новых серий нет уже {_fmt(at - last_new)}")
    return problems


async def _main() -> int:
    from app.db import engine, session
    async with session() as s:
        problems = await check(s)
    await engine.dispose()
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
