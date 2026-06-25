"""Read-репозиторий задач: FTS-поиск и чтение поддерева (TASK-09, HIER-01/02/03).

Только ЧТЕНИЕ — событий НЕ пишет (read-side, D-14..D-17). Модуль узкий и
session-injected (как event_repository): транзакцию НЕ открывает. Весь SQL — через
SQLAlchemy Core с bind-параметрами; пользовательский ввод (`q`, фильтры) НИКОГДА не
конкатенируется в строку SQL/tsquery (security V5, T-02-10/11).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import ColumnElement, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import Task

# Потолок размера выдачи поиска (D-16): даже limit>200 ограничивается этим (DoS-guard).
_SEARCH_LIMIT_CEILING = 200
_SEARCH_LIMIT_DEFAULT = 50
# Конфиг FTS — ДОЛЖЕН совпадать с search_vector в migration 002 / models.py (A1).
_FTS_CONFIG = "russian"


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Канонический ридер задачи по id — read, без события (TASK-04)."""
    return await session.get(Task, task_id)


async def search_tasks(
    session: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    parent_task_id: uuid.UUID | None = None,
    blocked: bool | None = None,
    energy: str | None = None,
    due_from: datetime.datetime | None = None,
    due_to: datetime.datetime | None = None,
    include_archived: bool = False,
    limit: int = _SEARCH_LIMIT_DEFAULT,
    offset: int = 0,
) -> list[Task]:
    """Серверный поиск задач: Postgres FTS + комбинируемые фильтры (TASK-09, D-14..17).

    FTS: при заданном ``q`` — ``websearch_to_tsquery('russian', :q) @@ search_vector``
    (q биндится параметром, не конкатенируется — V5). Сортировка: по релевантности
    (ts_rank) затем created_at DESC при ``q``, иначе created_at DESC (D-16).
    Пагинация: ``limit`` (дефолт 50, потолок 200) + ``offset`` (D-16).

    Правило статуса/архива (A2): если задан ``status`` — он ПОБЕЖДАЕТ ``blocked``
    (явный статус сильнее) и фильтр blocked игнорируется. Если ``status is None``:
    ``blocked=True`` → status=='BLOCKED', ``blocked=False`` → status!='BLOCKED'.
    ARCHIVED исключены по умолчанию (D-15); вернуть их можно явным ``status='ARCHIVED'``
    или ``include_archived=True``.

    Returns:
        Список задач (может быть пустым). Чтение — событий не пишет.
    """
    tsq = func.websearch_to_tsquery(_FTS_CONFIG, q) if q is not None else None

    conds: list[ColumnElement[bool]] = []
    if tsq is not None:
        conds.append(Task.search_vector.op("@@")(tsq))

    if status is not None:
        conds.append(Task.status == status)  # A2: явный статус побеждает blocked
    else:
        if blocked is True:
            conds.append(Task.status == "BLOCKED")
        elif blocked is False:
            conds.append(Task.status != "BLOCKED")
        if not include_archived:
            conds.append(Task.status != "ARCHIVED")  # D-15

    if parent_task_id is not None:
        conds.append(Task.parent_task_id == parent_task_id)
    if energy is not None:
        conds.append(Task.energy_required == energy)
    if due_from is not None:
        conds.append(Task.due_date >= due_from)
    if due_to is not None:
        conds.append(Task.due_date <= due_to)

    stmt = select(Task).where(*conds)
    if tsq is not None:
        stmt = stmt.order_by(
            desc(func.ts_rank(Task.search_vector, tsq)), desc(Task.created_at)
        )
    else:
        stmt = stmt.order_by(desc(Task.created_at))
    stmt = stmt.limit(min(limit, _SEARCH_LIMIT_CEILING)).offset(offset)

    result = await session.execute(stmt)
    return list(result.scalars().all())
