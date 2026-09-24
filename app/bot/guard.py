"""Защита бота. Главный дефицит — единственный рабочий IP к сайту, поэтому
лимитируется не «сообщения вообще», а действия, которые ведут к запросу на сайт.
Всё в памяти процесса: при одной реплике бота этого достаточно."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

MAX_QUERY_LEN = 100
MAX_SUBSCRIPTIONS = 100
MAX_VOICES = 30              # озвучек в одном фильтре: больше не бывает у самых популярных тайтлов
MAX_LINK_SCAN = 512          # ссылку ищем в начале сообщения: ссылка HDREZKA короче, а регулярка на 4096
                             # символах из одних «/» занимала 220 мс — всё это время бот стоит для всех
SITE_TIMEOUT = 25            # сек ожидания ответа сайта, дальше — «попробуйте позже»
TEXT_MAX = 4000              # лимит Telegram 4096 считается после разбора разметки и в UTF-16: запас

# Управляющие и невидимые символы: нулевой ширины, направление текста (в т. ч. изоляты U+2066–2069), BOM.
# Экранированы: невидимые символы в исходнике прячут, что на самом деле написано (ruff PLE2502/2515).
_CONTROL_RX = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_SPACES_RX = re.compile(r"\s+")


def clean_query(text: str) -> str:
    text = _CONTROL_RX.sub("", text or "")
    return _SPACES_RX.sub(" ", text).strip()[:MAX_QUERY_LEN]


class UserLimiter:
    """Скользящее окно: не больше `per_minute` за 60 с и `per_hour` за 3600 с."""

    def __init__(self, per_minute: int, per_hour: int) -> None:
        self.per_minute, self.per_hour = per_minute, per_hour
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._calls = 0

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits[user_id]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour or sum(1 for t in q if now - t <= 60) >= self.per_minute:
            return False
        q.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:          # не даём словарю расти бесконечно
            for uid in [u for u, d in self._hits.items() if not d or now - d[-1] > 3600]:
                self._hits.pop(uid, None)
        return True


site_actions = UserLimiter(per_minute=6, per_hour=60)     # поиск, ссылка — каждый = запрос к сайту
cheap_actions = UserLimiter(per_minute=30, per_hour=600)  # кнопки, /my — только база
feedback_actions = UserLimiter(per_minute=3, per_hour=15)  # письма автору — чтобы не завалили
# Общий предел на все апдейты человека — поверх пределов отдельных действий: нестандартный клиент или
# скрипт не должен загрузить бота, базу и общий лимит Telegram на исходящие (~30 сообщений/с на бота).
flood = UserLimiter(per_minute=40, per_hour=1000)
flood_log = UserLimiter(per_minute=1, per_hour=10)          # строка в лог о флуде — не чаще раза в минуту
