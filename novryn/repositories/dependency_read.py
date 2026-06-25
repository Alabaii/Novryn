"""Read-репозиторий зависимостей/вложений/сессий (DEP/ATCH/SESS read-tier).

Только ЧТЕНИЕ — событий не пишет. Reachability-CTE (``is_reachable``) обслуживает
проверку циклов сервиса зависимостей (вызывается ВНУТРИ транзакции INSERT'а, D-10).
Читатели возвращают только АКТИВНЫЕ строки (``deleted_at IS NULL``, D-10) — soft-deleted
история в выдачу не попадает. Session injected, транзакцию не открывает.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import Attachment, Session, TaskDependency


async def is_reachable(
    session: AsyncSession, *, start_id: uuid.UUID, target_id: uuid.UUID
) -> bool:
    """True, если из ``start_id`` достижим ``target_id`` по АКТИВНЫМ рёбрам (D-10).

    Рёбра: ``TaskDependency(task_id=X, depends_on_task_id=Y)`` = «X зависит от Y», обход
    идёт X→Y. Используется для детекции цикла перед вставкой ребра (A→B): если из B уже
    достижим A — новое ребро замкнёт цикл. ``UNION`` (не ALL) обрывает обход по уже
    посещённым узлам (терминирует на циклах графа, T-02-18). Учитываются только активные
    рёбра (``deleted_at IS NULL``).
    """
    anchor = (
        select(TaskDependency.depends_on_task_id.label("node"))
        .where(TaskDependency.task_id == start_id)
        .where(TaskDependency.deleted_at.is_(None))
        .cte("reach", recursive=True)
    )
    nxt = (
        select(TaskDependency.depends_on_task_id.label("node"))
        .join(anchor, TaskDependency.task_id == anchor.c.node)
        .where(TaskDependency.deleted_at.is_(None))
    )
    reach = anchor.union(nxt)

    stmt = select(reach.c.node).where(reach.c.node == target_id).limit(1)
    result = await session.execute(stmt)
    return result.first() is not None


async def get_dependencies(
    session: AsyncSession, task_id: uuid.UUID
) -> list[TaskDependency]:
    """Активные зависимости задачи (``deleted_at IS NULL``) — read, без события (DEP-03)."""
    result = await session.execute(
        select(TaskDependency)
        .where(TaskDependency.task_id == task_id)
        .where(TaskDependency.deleted_at.is_(None))
    )
    return list(result.scalars().all())


async def get_resources(session: AsyncSession, task_id: uuid.UUID) -> list[Attachment]:
    """Активные вложения задачи (``deleted_at IS NULL``) — read, без события (ATCH-04)."""
    result = await session.execute(
        select(Attachment)
        .where(Attachment.task_id == task_id)
        .where(Attachment.deleted_at.is_(None))
    )
    return list(result.scalars().all())


async def get_sessions(session: AsyncSession, task_id: uuid.UUID) -> list[Session]:
    """Все сессии задачи в порядке started_at, id ASC — read, без события (SESS-04/A7).

    Сессии НЕ soft-delete'ятся (deleted_at у них нет) — это историческая запись попыток;
    возвращаются все. Сортировка по started_at затем id (UUID v7) по возрастанию — самые
    старые первыми (A7).
    """
    result = await session.execute(
        select(Session)
        .where(Session.task_id == task_id)
        .order_by(Session.started_at, Session.id)
    )
    return list(result.scalars().all())
