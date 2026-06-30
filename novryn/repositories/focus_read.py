"""Read-репозиторий daily focus: чтение последней версии снимка (FOCUS-02/04/05).

Только ЧТЕНИЕ — событий не пишет, транзакцию не открывает, session injected
(зеркало правил ``dependency_read``). Все фильтры — bind-параметры SQLAlchemy
Core (``.where(DailyFocus.date == date)``), конкатенации в SQL нет (T-03-06).

«Последняя версия» (D-02): на одну дату может быть несколько снимков (повторный
``generate_daily_focus`` пишет новую версию с новым ``focus_set_id``). Версия
идентифицируется ``focus_set_id`` (один UUID на снимок), НЕ ``generated_at``:
выбирается ``focus_set_id`` снимка с ``MAX(generated_at)`` (tie-break
``focus_set_id DESC`` при равном ``generated_at`` — CR-01), затем берутся ТОЛЬКО
строки этого ``focus_set_id`` (поддержано индексом
``(date, generated_at DESC, focus_set_id DESC, rank, id DESC)`` миграции 003).
Строки версии упорядочены по ``rank ASC`` (FOCUS-02).

Решение D-04 (ошибка на пустоту, НЕ пустой список) поднимается здесь, в
read-обёртке: для даты без снимка обе функции бросают ``FocusNotFoundError``.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from novryn.db.models import DailyFocus
from novryn.domain.errors import FocusNotFoundError


def _latest_version_stmt(date: datetime.date) -> Select[tuple[DailyFocus]]:
    """SELECT строк последней версии снимка на ``date``, упорядоченных по rank ASC.

    Версия = ``focus_set_id`` (один UUID на снимок), НЕ ``generated_at``. Подзапрос
    выбирает ``focus_set_id`` снимка с максимальным ``generated_at`` (tie-break
    ``focus_set_id DESC`` при равном ``generated_at`` — детерминированно), затем
    основной запрос берёт ТОЛЬКО строки этого ``focus_set_id``. Это исключает
    смешивание двух версий при равном ``generated_at`` (CR-01: один тик часов /
    NTP-сдвиг назад). ``date`` — bind-параметр Core (T-03-06).
    """
    latest_set = (
        select(DailyFocus.focus_set_id)
        .where(DailyFocus.date == date)
        .order_by(DailyFocus.generated_at.desc(), DailyFocus.focus_set_id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(DailyFocus)
        .where(DailyFocus.date == date, DailyFocus.focus_set_id == latest_set)
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
