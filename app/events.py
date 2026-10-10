"""Действия людей в боте — чтобы видеть, где новички теряются (10.10.2026).

Пишем только тип действия и короткую метку: команда, кнопка меню, имя нажатой кнопки, итог поиска или ссылки.
Тексты сообщений и поисковых запросов не храним — как и тексты писем автору (app/feedback.py). Хранение —
KEEP_DAYS: старое удаляет отправщик раз в час (purge). Смотреть — /events у админа.

    kind      detail
    cmd       /start, /start ссылка, /my …          — команда, без аргументов
    menu      btn_find, btn_my …                    — кнопка нижнего меню
    text      —                                     — текст: поиск, ссылка или письмо автору (сам текст не пишем)
    media     photo, sticker, voice …               — не текст
    button    sub, subf, set, startlang …           — нажатая кнопка: имя из CB в app/bot/main.py, без id
    search    catalog:N, site:N, short, busy, limit — чем кончился поиск
    link      known, read, gone, busy, limit        — чем кончилась присланная ссылка
    feedback  bug, idea, collab, other              — письмо автору отправлено
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db import session
from app.i18n import t

log = logging.getLogger(__name__)

KEEP_DAYS = 30
SUMMARY_DAYS = 7
SUMMARY_USERS = 15
SUMMARY_TAIL = 8          # последних действий в строке сводки
PATH_LIMIT = 80           # действий в пути одного человека


async def record(user_id: int, kind: str, detail: str | None = None) -> None:
    """Своя короткая транзакция. Сбой записи не мешает ответить человеку: только строка в лог."""
    try:
        async with session() as s:
            await s.execute(text("INSERT INTO user_events (user_id, kind, detail) VALUES (:u, :k, :d)"),
                            {"u": user_id, "k": kind[:16], "d": detail[:32] if detail else None})
            await s.commit()
    except Exception as exc:
        log.warning("Событие %s/%s человека %s не записано: %s", kind, detail, user_id, exc)


def found(where: str, n: int) -> str:
    """Итог поиска: «catalog:3», «site:0». Собирается здесь, а не строкой в app/bot/main.py: там строки вида «имя:…» —
    это callback_data кнопок, и tests/test_security.py проверяет, что у каждой есть шаблон."""
    return f"{where}:{n}"


async def purge(s) -> int:
    r = await s.execute(text("DELETE FROM user_events WHERE at < now() - make_interval(days => :d)"),
                        {"d": KEEP_DAYS})
    return r.rowcount or 0


# ----------------------------------------------------------------------------- /events

_BUTTONS = {
    "startlang": "язык при старте", "site": "искать на сайте", "sub": "подписаться", "subf": "франшиза",
    "subf_all": "подписка на франшизу", "sched": "расписание", "pcard": "карточка", "fcard": "карточка франшизы",
    "voices": "озвучки", "vt": "озвучка вкл/выкл", "vany": "любая озвучка", "card": "карточка подписки",
    "my": "мои подписки: листает", "unsub": "отписался", "unsubq": "«отписаться?»", "keep": "оставил подписку",
    "set": "настройки", "setq": "тихие часы", "setl": "язык", "setv": "озвучка по умолчанию",
    "fb": "написать автору", "fb_topic": "тема письма", "fb_cancel": "письмо: отмена", "noop": "пустая кнопка",
    "stale": "старая кнопка",
}
_OUTCOMES = {
    "short": "слишком короткий запрос", "busy": "сайт не ответил", "limit": "упёрся в лимит",
    "known": "карточка из базы", "read": "прочитана с сайта", "gone": "страницы нет на сайте",
}


def label(kind: str, detail: str | None) -> str:
    if kind == "cmd":
        return detail or "команда"
    if kind == "menu":
        return f"меню «{t('ru', detail)}»" if detail and detail.startswith("btn_") else f"меню {detail}"
    if kind == "text":
        return "текст"
    if kind == "media":
        return f"не текст ({detail})"
    if kind == "button":
        return f"кнопка «{_BUTTONS.get(detail or '', detail)}»"
    if kind in ("search", "link"):
        where, _, n = (detail or "").partition(":")
        if where in ("catalog", "site"):
            found = f"нашлось {n}" if n not in ("", "0") else "ничего"
            result = f"{found} ({'каталог' if where == 'catalog' else 'сайт'})"
        else:
            result = _OUTCOMES.get(where, where)
        return f"{'поиск' if kind == 'search' else 'ссылка'}: {result}"
    if kind == "feedback":
        return f"письмо автору отправлено ({detail})"
    return f"{kind} {detail or ''}".strip()


def _who(user_id: int, username: str | None, created: datetime | None, tz: int) -> str:
    name = f"@{html.escape(username)} · " if username else ""
    since = f" · в боте с {(created + timedelta(hours=tz)):%d.%m}" if created else " · ещё не в users"
    return f"👤 {name}<code>{user_id}</code>{since}"


async def summary(s, tz: int, at: datetime | None = None) -> str:
    """Кто что делал за SUMMARY_DAYS: на человека — число действий, время последнего и хвост пути."""
    rows = (await s.execute(text("""
        WITH recent AS (
            SELECT user_id, count(*) AS n, max(at) AS last_at FROM user_events
             WHERE at > coalesce(CAST(:at AS timestamptz), now()) - make_interval(days => :days)
             GROUP BY user_id ORDER BY max(at) DESC LIMIT :users)
        SELECT r.user_id, r.n, r.last_at, u.username, u.created_at,
               (SELECT array_agg(e.kind || '|' || coalesce(e.detail, '') ORDER BY e.at)
                  FROM (SELECT kind, detail, at FROM user_events
                         WHERE user_id = r.user_id ORDER BY at DESC LIMIT :tail) e) AS tail
          FROM recent r LEFT JOIN users u ON u.id = r.user_id
         ORDER BY r.last_at DESC"""),
        {"at": at, "days": SUMMARY_DAYS, "users": SUMMARY_USERS, "tail": SUMMARY_TAIL})).all()
    if not rows:
        return f"За {SUMMARY_DAYS} дней действий в боте нет."
    lines = [f"<b>Действия за {SUMMARY_DAYS} дней</b> (хранятся {KEEP_DAYS})"]
    for user_id, n, last_at, username, created, tail in rows:
        steps = " → ".join(html.escape(label(*item.split("|", 1))) for item in tail)
        lines += ["", _who(user_id, username, created, tz),
                  f"{n} действий, последнее {(last_at + timedelta(hours=tz)):%d.%m %H:%M}", f"… {steps}"]
    lines += ["", "Путь одного человека: /events &lt;id&gt;"]
    return "\n".join(lines)


async def path(s, user_id: int, tz: int) -> str:
    """Последние PATH_LIMIT действий человека по порядку, с датами."""
    rows = (await s.execute(text("""
        SELECT at, kind, detail FROM (SELECT at, kind, detail FROM user_events WHERE user_id = :u
                                       ORDER BY at DESC LIMIT :n) e ORDER BY at"""),
        {"u": user_id, "n": PATH_LIMIT})).all()
    user = (await s.execute(text("SELECT username, created_at FROM users WHERE id = :u"), {"u": user_id})).first()
    head = _who(user_id, user[0] if user else None, user[1] if user else None, tz)
    if not rows:
        return f"{head}\nДействий за {KEEP_DAYS} дней нет."
    lines, day = [f"{head} — последние {len(rows)} действий"], None
    for at, kind, detail in rows:
        local = at + timedelta(hours=tz)
        if local.date() != day:
            day = local.date()
            lines.append(f"\n<b>{local:%d.%m}</b>")
        lines.append(f"{local:%H:%M} {html.escape(label(kind, detail))}")
    return "\n".join(lines)
