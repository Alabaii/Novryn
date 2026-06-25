"""Read-репозиторий задач: FTS-поиск и чтение поддерева (TASK-09, HIER-01/02/03).

Только ЧТЕНИЕ — событий НЕ пишет (read-side, D-14..D-17). Модуль узкий и
session-injected (как event_repository): транзакцию НЕ открывает. Весь SQL — через
SQLAlchemy Core с bind-параметрами; пользовательский ввод (`q`, фильтры) НИКОГДА не
конкатенируется в строку SQL/tsquery (security V5, T-02-10/11).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import ColumnElement, RowMapping, desc, func, literal, select
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


async def fetch_subtree_rows(
    session: AsyncSession, root_id: uuid.UUID, max_depth: int
) -> list[RowMapping]:
    """Получить ВСЁ поддерево root_id за ОДИН SQL-запрос (рекурсивный CTE, HIER-02).

    Anchor: корень с ``depth=0``. Рекурсия: дети (``parent_task_id == родитель``) с
    ``depth+1``, ограничена ``WHERE depth < max_depth`` (guard разбега, D-12). Возврат —
    ПЛОСКИЙ список mappings (id, parent_task_id, title, status, depth) из ОДНОГО
    ``execute`` (no N+1). Статус НЕ фильтруется (ARCHIVED включены, D-13); вложенность
    собирает сервис в памяти — IO остаётся одним запросом.

    Note:
        ``max_depth`` вызывающий обычно передаёт как ``реальный_лимит + 1`` — чтобы
        отличить «глубже лимита» от «ровно лимит» (см. hierarchy_service.get_task_tree).
    """
    anchor = (
        select(
            Task.id,
            Task.parent_task_id,
            Task.title,
            Task.status,
            literal(0).label("depth"),
        )
        .where(Task.id == root_id)
        .cte("subtree", recursive=True)
    )
    child = (
        select(
            Task.id,
            Task.parent_task_id,
            Task.title,
            Task.status,
            (anchor.c.depth + 1).label("depth"),
        )
        .join(anchor, Task.parent_task_id == anchor.c.id)
        .where(anchor.c.depth < max_depth)
    )
    subtree = anchor.union_all(child)

    stmt = select(subtree).order_by(subtree.c.depth)
    result = await session.execute(stmt)
    return list(result.mappings().all())
