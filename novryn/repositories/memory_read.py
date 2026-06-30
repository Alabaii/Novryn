"""Read-репозиторий памяти: russian FTS-поиск по content + фильтры (MEM-03, D-09).

Только ЧТЕНИЕ — событий НЕ пишет (read-side). Модуль узкий и session-injected
(как ``task_read``/``event_repository``): транзакцию НЕ открывает. Весь SQL — через
SQLAlchemy Core с bind-параметрами; пользовательский ``q`` и структурные фильтры
НИКОГДА не конкатенируются в строку SQL/tsquery (security V5, T-03-07) — ``q`` идёт
в ``websearch_to_tsquery('russian', :q)`` как bind-параметр.

Зеркало ``task_read.search_tasks`` под ОДИН текстовый источник ``content``: при
заданном ``q`` — FTS-предикат + сортировка по ``ts_rank``; структурные фильтры
(``memory_type``/``min_confidence``/``source``) комбинируются с FTS и друг с другом
(любое подмножество). Пагинация: ``limit`` (дефолт 50, потолок 200) + ``offset``.
"""

from __future__ import annotations

import decimal

from sqlalchemy import ColumnElement, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import UserMemory

# Потолок размера выдачи поиска: даже limit>200 ограничивается этим (DoS-guard).
_SEARCH_LIMIT_CEILING = 200
_SEARCH_LIMIT_DEFAULT = 50
# Конфиг FTS — ДОЛЖЕН совпадать с search_vector в migration 003 / models.py.
_FTS_CONFIG = "russian"


async def memory_search(
    session: AsyncSession,
    *,
    q: str | None = None,
    memory_type: str | None = None,
    min_confidence: float | decimal.Decimal | None = None,
    source: str | None = None,
    limit: int = _SEARCH_LIMIT_DEFAULT,
    offset: int = 0,
) -> list[UserMemory]:
    """Серверный поиск памяти: russian FTS по content + комбинируемые фильтры (MEM-03).

    FTS: при заданном ``q`` — ``websearch_to_tsquery('russian', :q) @@ search_vector``
    (q биндится параметром, не конкатенируется — V5). Сортировка: по релевантности
    (``ts_rank``) затем ``created_at`` DESC при ``q``, иначе ``created_at`` DESC.

    Структурные фильтры комбинируются с FTS и друг с другом (D-09): ``memory_type``
    (точное совпадение), ``min_confidence`` (``confidence >= min_confidence``),
    ``source`` (точное совпадение). Пагинация: ``limit`` (дефолт 50, потолок 200)
    + ``offset``.

    Returns:
        Список строк памяти (может быть пустым). Чтение — событий не пишет.
    """
    tsq = func.websearch_to_tsquery(_FTS_CONFIG, q) if q is not None else None

    conds: list[ColumnElement[bool]] = []
    if tsq is not None:
        conds.append(UserMemory.search_vector.op("@@")(tsq))
    if memory_type is not None:
        conds.append(UserMemory.memory_type == memory_type)
    if min_confidence is not None:
        conds.append(UserMemory.confidence >= min_confidence)
    if source is not None:
        conds.append(UserMemory.source == source)

    stmt = select(UserMemory).where(*conds)
    if tsq is not None:
        stmt = stmt.order_by(
            desc(func.ts_rank(UserMemory.search_vector, tsq)),
            desc(UserMemory.created_at),
        )
    else:
        stmt = stmt.order_by(desc(UserMemory.created_at))
    stmt = stmt.limit(min(limit, _SEARCH_LIMIT_CEILING)).offset(offset)

    result = await session.execute(stmt)
    return list(result.scalars().all())
