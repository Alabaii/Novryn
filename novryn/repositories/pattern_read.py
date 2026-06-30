"""Read-репозиторий поведенческих паттернов: фильтры + сортировка, БЕЗ FTS (PAT-02, D-10).

Только ЧТЕНИЕ — событий НЕ пишет (read-side). Модуль узкий и session-injected
(как ``dependency_read``/``task_read``): транзакцию НЕ открывает. Весь SQL — через
SQLAlchemy Core с bind-параметрами; ``pattern_type``/``min_confidence`` идут как
bind-параметры (``==`` / ``>=``), НИКОГДА не конкатенируются в строку SQL (V5,
T-03-12).

В отличие от ``memory_read``, FTS здесь НЕТ (D-10): у паттерна нет текстового
content — только структурированный ``evidence_json``. Поэтому простые фильтры
``pattern_type``/``min_confidence`` + сортировка по ``confidence`` затем
``created_at`` DESC; никаких ``websearch_to_tsquery``/``ts_rank``/``search_vector``.
"""

from __future__ import annotations

import decimal

from sqlalchemy import ColumnElement, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import BehaviorPattern


async def get_behavior_patterns(
    session: AsyncSession,
    *,
    pattern_type: str | None = None,
    min_confidence: float | decimal.Decimal | None = None,
) -> list[BehaviorPattern]:
    """Чтение паттернов: фильтры ``pattern_type``/``min_confidence`` + сортировка (PAT-02).

    Фильтры комбинируются и опциональны (D-10): ``pattern_type`` (точное совпадение),
    ``min_confidence`` (``confidence >= min_confidence``). ``None`` пропускается.
    Сортировка — ``confidence`` DESC, затем ``created_at`` DESC. БЕЗ FTS.

    Returns:
        Список строк паттернов (может быть пустым). Чтение — событий не пишет.
    """
    conds: list[ColumnElement[bool]] = []
    if pattern_type is not None:
        conds.append(BehaviorPattern.pattern_type == pattern_type)
    if min_confidence is not None:
        # WR-05: нормализуем к Decimal до бинда. confidence — NUMERIC(3,2); float
        # (например 0.7, непредставимый в двоичном float) дал бы граничные
        # расхождения на крае (== 0.70 попадает/выпадает непредсказуемо).
        # Decimal(str(...)) сохраняет десятичную точность фильтра на чтении.
        mc = (
            min_confidence
            if isinstance(min_confidence, decimal.Decimal)
            else decimal.Decimal(str(min_confidence))
        )
        conds.append(BehaviorPattern.confidence >= mc)

    stmt = (
        select(BehaviorPattern)
        .where(*conds)
        .order_by(desc(BehaviorPattern.confidence), desc(BehaviorPattern.created_at))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
