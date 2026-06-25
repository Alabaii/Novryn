"""Сервис зависимостей задач (PRD §5; DEP-01..05).

``link_tasks`` отклоняет self-dependency (DEP-04) ДО UoW и цикл (DEP-05) ВНУТРИ
транзакции INSERT'а — reachability-проверка по активным рёбрам в той же
``session.begin()``, поэтому при цикле откатывается и строка, и событие (ROADMAP #3,
D-10). ``unlink_tasks`` — soft-delete активного ребра (D-04/D-07) с идемпотентным no-op
(D-08). Повторная привязка — всегда НОВАЯ строка (link_tasks), старая soft-deleted
остаётся в истории (D-07).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import TaskDependency
from novryn.domain.errors import CycleError, SelfDependencyError
from novryn.domain.events import EventType
from novryn.repositories.dependency_read import is_reachable
from novryn.repositories.uow import mutate_with_event


async def link_tasks(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    depends_on_task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """Создать ребро «task_id зависит от depends_on_task_id» + dependency.linked (DEP-01).

    Self-dependency отклоняется ``SelfDependencyError`` ДО UoW (DEP-04). Внутри UoW,
    ПЕРЕД INSERT'ом, проверяется цикл: если из ``depends_on_task_id`` уже достижим
    ``task_id`` — ``CycleError`` (откат всей транзакции: ни строки, ни события — DEP-05).

    Returns:
        id созданного ребра.

    Raises:
        SelfDependencyError: при task_id == depends_on_task_id (DEP-04).
        CycleError: если ребро замкнуло бы цикл (DEP-05).
    """
    if task_id == depends_on_task_id:
        raise SelfDependencyError(task_id)  # DEP-04, до UoW
    dep_id = new_id()

    async def apply(s: AsyncSession) -> uuid.UUID:
        # Цикл-проверка В ТОЙ ЖЕ транзакции, до INSERT (DEP-05/ROADMAP #3): если из
        # depends_on_task_id достижим task_id — новое ребро замкнёт цикл.
        if await is_reachable(s, start_id=depends_on_task_id, target_id=task_id):
            raise CycleError(task_id, depends_on_task_id)
        await s.execute(
            insert(TaskDependency).values(
                id=dep_id, task_id=task_id, depends_on_task_id=depends_on_task_id
            )
        )
        return dep_id

    await mutate_with_event(
        session,
        entity_type="dependency",
        entity_id=dep_id,
        event_type=EventType.DEPENDENCY_LINKED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(TaskDependency, dep_id),
    )
    return dep_id


async def unlink_tasks(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    depends_on_task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Soft-delete активного ребра + dependency.unlinked; no-op если ребра нет (D-08).

    Читает АКТИВНОЕ ребро (deleted_at IS NULL). Нет → ничего не пишет (идемпотентный
    no-op, D-08), возвращает None. Есть → ставит deleted_at=now() через UoW (D-04/D-07).

    Returns:
        id soft-deleted ребра, либо None при no-op.
    """
    row = (
        await session.execute(
            select(TaskDependency)
            .where(TaskDependency.task_id == task_id)
            .where(TaskDependency.depends_on_task_id == depends_on_task_id)
            .where(TaskDependency.deleted_at.is_(None))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        await session.rollback()  # закрыть read-autobegin
        return None  # no-op, без события (D-08)
    dep_id = row.id
    await session.rollback()  # закрыть read-autobegin перед явным begin() UoW

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            update(TaskDependency)
            .where(TaskDependency.id == dep_id)
            .values(deleted_at=func.now())
        )
        return dep_id

    await mutate_with_event(
        session,
        entity_type="dependency",
        entity_id=dep_id,
        event_type=EventType.DEPENDENCY_UNLINKED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(TaskDependency, dep_id),
    )
    return dep_id
