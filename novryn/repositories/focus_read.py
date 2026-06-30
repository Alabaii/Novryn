"""Read-репозиторий daily focus: чтение последней версии снимка (FOCUS-02/04/05).

Только ЧТЕНИЕ — событий не пишет, транзакцию не открывает, session injected
(зеркало правил ``dependency_read``). Все фильтры — bind-параметры SQLAlchemy
Core (``.where(DailyFocus.date == date)``), конкатенации в SQL нет (T-03-06).

«Последняя версия» (D-02): на одну дату может быть несколько снимков (повторный
``generate_daily_focus`` пишет новую версию с большим ``generated_at``).
Выбирается версия с ``MAX(generated_at)`` на дату; при равном ``generated_at``
tie-break ``id DESC`` (Pitfall 5, поддержан индексом
``(date, generated_at DESC, id DESC)`` из миграции 003). Строки версии
упорядочены по ``rank ASC`` (FOCUS-02).

Решение D-04 (ошибка на пустоту, НЕ пустой список) поднимается здесь, в
read-обёртке: для даты без снимка обе функции бросают ``FocusNotFoundError``.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from novryn.db.models import DailyFocus
from novryn.domain.errors import FocusNotFoundError


def _latest_version_stmt(date: datetime.date) -> Select[tuple[DailyFocus]]:
    """SELECT строк последней версии снимка на ``date``, упорядоченных по rank ASC.

    Подзапрос ``MAX(generated_at)`` фиксирует последнюю версию; основной запрос
    берёт только её строки. ``date`` — bind-параметр Core (T-03-06).
    """
    latest = (
        select(func.max(DailyFocus.generated_at))
        .where(DailyFocus.date == date)
        .scalar_subquery()
    )
    return (
        select(DailyFocus)
        .where(DailyFocus.date == date, DailyFocus.generated_at == latest)
        .order_by(DailyFocus.rank, DailyFocus.id.desc())
    )


async def get_today_tasks(
    session: AsyncSession, date: datetime.date
) -> list[DailyFocus]:
    """Строки последней версии снимка на ``date`` в порядке rank ASC (FOCUS-02).

    Сохраняет ``reason``/``generated_by`` каждой строки (FOCUS-04). При двух
    версиях одной даты возвращается ТОЛЬКО последняя (MAX(generated_at)).
    Для даты без снимка → ``FocusNotFoundError`` (D-04), НЕ пустой список.
    """
    rows = list((await session.execute(_latest_version_stmt(date))).scalars().all())
    if not rows:
        raise FocusNotFoundError(date)
    return rows


async def focus_now(session: AsyncSession, date: datetime.date) -> DailyFocus:
    """Задача с наивысшим рангом (минимальный rank) последней версии (FOCUS-05).

    ``order_by(rank ASC)`` → первая строка имеет наивысший приоритет. Для даты
    без снимка → ``FocusNotFoundError`` (D-04), НЕ None.
    """
    result = await session.execute(_latest_version_stmt(date).limit(1))
    row: DailyFocus | None = result.scalar_one_or_none()
    if row is None:
        raise FocusNotFoundError(date)
    return row
