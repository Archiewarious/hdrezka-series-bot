"""Локальная выдача поиска: группировка результатов каталога по франшизам
(docs/PRODUCT_AND_SCALE.md §7.3). Чистые функции — покрыты тестами."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Page

MAX_STANDALONE = 5
MAX_WAITING = 3        # завершённые без франшизы: карточка с «🔔 Сообщить о продолжении»


def is_ongoing(p: Page) -> bool:
    """Выходит: не завершён, не фильм, серии уже были (объявленные без серий не предлагаем)."""
    return not p.is_finished and p.content_type != "film" and p.last_episode is not None


@dataclass
class Grouped:
    franchise_ids: list[int] = field(default_factory=list)       # в порядке релевантности
    origin: dict[int, int] = field(default_factory=dict)         # franchise_id → id самой релевантной части
    standalone: list[Page] = field(default_factory=list)         # выходящие страницы без франшизы
    waiting: list[Page] = field(default_factory=list)            # завершённые сериалы без франшизы («жду продолжения»)
    hidden: int = 0                                              # фильмы и страницы без серий вне франшиз

    @property
    def empty(self) -> bool:
        return not self.franchise_ids and not self.standalone and not self.waiting


def group_hits(pages: list[Page], max_standalone: int = MAX_STANDALONE, max_waiting: int = MAX_WAITING) -> Grouped:
    """Части одной франшизы схлопываются в одну строку — человек хочет «всё новое по тайтлу»,
    а не выбирать между ТВ-1 и ТВ-4. Завершённые сериалы без франшизы показываем отдельно:
    на них можно «ждать продолжения» (§7, решение 3). Фильмы и страницы без серий скрываем, но считаем."""
    g = Grouped()
    for p in pages:
        if p.franchise_id:
            if p.franchise_id not in g.origin:
                g.franchise_ids.append(p.franchise_id)
                g.origin[p.franchise_id] = p.id
        elif is_ongoing(p):
            if len(g.standalone) < max_standalone:
                g.standalone.append(p)
        elif p.is_finished and p.content_type != "film":
            if len(g.waiting) < max_waiting:
                g.waiting.append(p)
            else:
                g.hidden += 1
        else:
            g.hidden += 1
    return g
