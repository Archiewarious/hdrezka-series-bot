"""Здоровье бота — одна проверка для /stats и для healthcheck контейнеров.

Сбой 05–10.09.2026 длился пять дней, потому что здоровьем считалось «цикл завершился». Здесь проверяется,
что события идут и что уведомления действительно уходят.

    python -m app.health            # всё: код выхода 1 и причины построчно
    python -m app.health events     # поллер: блок разбирается, новые серии появляются
    python -m app.health delivery   # отправщик: уведомления не залёживаются и не падают
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
DELIVERY_LATE = timedelta(minutes=15)    # срок наступил, а уведомление не ушло


def _fmt(d: timedelta) -> str:
    hours = int(d.total_seconds() // 3600)
    return f"{hours} ч" if hours else f"{int(d.total_seconds() // 60)} мин"


async def _events(s, at: datetime) -> list[str]:
    problems: list[str] = []
    for key, limit, what in (("last_poll_ok", POLL_STALE, "поллер не завершал цикл"),
                             ("updates_ok_at", EVENTS_STALE, "блок обновлений не разбирается")):
        value = await svc.meta_get(s, key)
        if not value:
            problems.append(f"{what}: ни разу")
        elif at - datetime.fromisoformat(value) > limit:
            problems.append(f"{what} уже {_fmt(at - datetime.fromisoformat(value))}")
    failed = int(await svc.meta_get(s, "updates_failed") or 0)
    if failed:
        problems.append(f"событий блока не разобрано в последнем проходе: {failed} — см. лог поллера")
    last_new = await s.scalar(text("SELECT max(first_seen_at) FROM episodes WHERE first_seen_at > CAST(:after AS timestamptz)"),
                              {"after": svc.SEED_BEFORE})
    if last_new is None:
        problems.append("новых серий не было ни разу")
    elif at - last_new > NO_NEW_EPISODES:
        problems.append(f"новых серий нет уже {_fmt(at - last_new)}")
    return problems


async def _delivery(s, at: datetime) -> list[str]:
    row = (await s.execute(text("""
        SELECT count(*) FILTER (WHERE status IN ('pending', 'sending') AND next_attempt_at < CAST(:late AS timestamptz)
                                  AND attempts = 0),
               count(*) FILTER (WHERE status IN ('pending', 'sending') AND attempts > 0 AND created_at < CAST(:late AS timestamptz)),
               count(*) FILTER (WHERE status = 'failed' AND coalesce(error, '') <> 'bot blocked'
                                  AND created_at > CAST(:day AS timestamptz))
          FROM notifications"""), {"late": at - DELIVERY_LATE, "day": at - timedelta(days=1)})).one()
    problems = []
    if row[0]:
        problems.append(f"уведомлений не взято в отправку, хотя срок прошёл: {row[0]} — отправщик не работает?")
    if row[1]:
        problems.append(f"уведомлений не уходит, повторы после сбоя: {row[1]} — см. лог отправщика")
    if row[2]:
        problems.append(f"уведомлений не отправилось за сутки: {row[2]} — см. notifications.error")
    return problems


async def check(s, at: datetime | None = None, part: str = "all") -> list[str]:
    at = at or svc.now()
    problems: list[str] = []
    if part in ("all", "events"):
        problems += await _events(s, at)
    if part in ("all", "delivery"):
        problems += await _delivery(s, at)
    return problems


async def _main(part: str) -> int:
    from app.db import engine, session
    async with session() as s:
        problems = await check(s, part=part)
    await engine.dispose()
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1] if len(sys.argv) > 1 else "all")))
