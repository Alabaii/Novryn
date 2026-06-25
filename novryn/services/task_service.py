"""Сервис жизненного цикла задач (PRD §4; TASK-01..11, HIER-01).

Каждая мутация проходит через UoW Фазы 1 с ЯВНЫМ бизнес-смысловым ``event_type``
(D-05). Lifecycle-переходы — ОТДЕЛЬНЫЕ методы, каждый пишет своё событие (D-01), а
НЕ status-роутер внутри ``update_task``. ``update_task`` пишет событие ТОЛЬКО при
реальном диффе (D-01/D-08 — нет фантомных событий). Bulk ``create_subtasks``
использует ``mutate_many_with_event`` (один ``task.created`` на ребёнка,
all-or-nothing — D-02/D-03).

Сервис получает СВЕЖУЮ ``AsyncSession`` от вызывающего и НИКОГДА не открывает свою
транзакцию (это делает UoW). Свободная AI-интерпретация живёт ТОЛЬКО в
``ai_context_json`` (TASK-11) — типизированных Hermes-колонок не вводим.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import Task
from novryn.domain.errors import NotFoundError
from novryn.domain.events import EventType
from novryn.repositories.uow import mutate_with_event


async def create_task(
    session: AsyncSession,
    *,
    title: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    description: str | None = None,
    **fields: Any,
) -> uuid.UUID:
    """Создать задачу со всеми PRD-полями + событие task.created (TASK-01/02/11).

    ``id`` генерируется ДО UoW (A3). Дополнительные PRD-поля (status, due_date,
    parent_task_id, *_time_estimate_minutes, energy_required, ai_context_json)
    передаются через ``**fields``. Свободная AI-интерпретация — только в
    ``ai_context_json`` (TASK-11), типизированных Hermes-колонок не добавляем.

    Returns:
        id созданной задачи.
    """
    task_id = new_id()

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            insert(Task).values(
                id=task_id, title=title, description=description, **fields
            )
        )
        return task_id

    await mutate_with_event(
        session,
        entity_type="task",
        entity_id=task_id,
        event_type=EventType.TASK_CREATED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Task, task_id),
    )
    return task_id


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Прочитать задачу по id — это read, событие НЕ пишется (TASK-04)."""
    return await session.get(Task, task_id)


async def update_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    changes: dict[str, Any],
) -> uuid.UUID:
    """Обновить поля задачи; task.updated ТОЛЬКО при реальном диффе (D-01/D-08).

    Загружает текущую строку; ``NotFoundError`` если её нет. Считает дифф (только
    реально меняющиеся поля) ДО UoW. Если дифф пуст — возврат без UoW: ни мутации,
    ни фантомного события (D-08). Иначе обновляет ровно изменённые поля через UoW
    с ``task.updated`` (D-01).

    Returns:
        ``task_id`` (как при изменении, так и при no-op).

    Raises:
        NotFoundError: если задачи с таким id нет.
    """
    current = await session.get(Task, task_id)
    if current is None:
        raise NotFoundError(task_id)
    diff = {k: v for k, v in changes.items() if getattr(current, k) != v}
    if not diff:
        return task_id  # no-op: ни мутации, ни события (D-08)

    # session.get выше авто-начал read-транзакцию; закрываем её, чтобы UoW открыл
    # свою явную session.begin() (иначе "transaction already begun"). Read-only —
    # откатывать нечего; before_row UoW перезагрузит свежим (CR-02: отдельный load).
    await session.rollback()

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(update(Task).where(Task.id == task_id).values(**diff))
        return task_id

    await mutate_with_event(
        session,
        entity_type="task",
        entity_id=task_id,
        event_type=EventType.TASK_UPDATED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Task, task_id),
    )
    return task_id
