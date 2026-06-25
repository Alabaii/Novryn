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

from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import Task
from novryn.domain.errors import NotFoundError
from novryn.domain.events import EventType
from novryn.repositories.uow import (
    MutationSpec,
    mutate_many_with_event,
    mutate_with_event,
)


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


async def _transition(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    values: dict[str, Any],
) -> uuid.UUID:
    """Общий каркас lifecycle-перехода: guard существования + UoW с ЯВНЫМ событием.

    Каждый публичный lifecycle-метод — отдельная операция со СВОИМ ``event_type``
    (D-01/D-05): этот хелпер лишь устраняет дублирование (guard + UoW + UPDATE).
    Событие НЕ выводится из dirty-трекинга. На несуществующей задаче — ``NotFoundError``
    (а не фантомное событие с before=after=None). НЕ каскадит на attachments/
    dependencies — Novryn хранит факты, очистка связей — решение Hermes (D-09).
    """
    if await session.get(Task, task_id) is None:
        raise NotFoundError(task_id)
    # session.get выше авто-начал read-tx; закрываем перед явным begin() UoW.
    await session.rollback()

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(update(Task).where(Task.id == task_id).values(**values))
        return task_id

    await mutate_with_event(
        session,
        entity_type="task",
        entity_id=task_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Task, task_id),
    )
    return task_id


async def complete_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """status=DONE + completed_at; событие task.completed (TASK-07, D-01)."""
    return await _transition(
        session,
        task_id=task_id,
        event_type=EventType.TASK_COMPLETED,
        actor_type=actor_type,
        actor_id=actor_id,
        values={"status": "DONE", "completed_at": func.now()},
    )


async def archive_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """status=ARCHIVED + archived_at; task.archived (TASK-08, D-01). НЕ каскадит (D-09)."""
    return await _transition(
        session,
        task_id=task_id,
        event_type=EventType.TASK_ARCHIVED,
        actor_type=actor_type,
        actor_id=actor_id,
        values={"status": "ARCHIVED", "archived_at": func.now()},
    )


async def block(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    reason: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """status=BLOCKED + blocked_reason=reason; событие task.blocked (Open Q3)."""
    return await _transition(
        session,
        task_id=task_id,
        event_type=EventType.TASK_BLOCKED,
        actor_type=actor_type,
        actor_id=actor_id,
        values={"status": "BLOCKED", "blocked_reason": reason},
    )


async def unblock(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """status=TODO + blocked_reason=None; task.unblocked (Open Q3: дефолт → TODO)."""
    return await _transition(
        session,
        task_id=task_id,
        event_type=EventType.TASK_UNBLOCKED,
        actor_type=actor_type,
        actor_id=actor_id,
        values={"status": "TODO", "blocked_reason": None},
    )


async def create_subtasks(
    session: AsyncSession,
    *,
    parent_task_id: uuid.UUID,
    subtasks: list[dict[str, Any]],
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Создать N подзадач под родителем в ОДНОЙ транзакции (TASK-10, D-02/D-03).

    На каждую подзадачу — свой ``MutationSpec`` (один ``task.created`` на ребёнка,
    D-02), все применяются через ``mutate_many_with_event`` в единственной
    транзакции. Любой сбой откатывает ВЕСЬ батч (D-03 all-or-nothing) — частичных
    подзадач и orphan-событий не остаётся. Привязка ``_tid``/``_sub`` через
    default-arg, чтобы замыкание брало значение ИТЕРАЦИИ (не late-binding).

    Returns:
        Список id созданных подзадач в порядке ``subtasks``.
    """
    specs: list[MutationSpec] = []
    new_ids: list[uuid.UUID] = []
    for sub in subtasks:
        tid = new_id()
        new_ids.append(tid)

        async def apply(
            s: AsyncSession,
            _tid: uuid.UUID = tid,
            _sub: dict[str, Any] = sub,
        ) -> uuid.UUID:
            await s.execute(
                insert(Task).values(id=_tid, parent_task_id=parent_task_id, **_sub)
            )
            return _tid

        async def load(s: AsyncSession, _tid: uuid.UUID = tid) -> Task | None:
            return await s.get(Task, _tid)

        specs.append(
            MutationSpec(
                entity_type="task",
                entity_id=tid,
                event_type=EventType.TASK_CREATED,
                actor_type=actor_type,
                actor_id=actor_id,
                apply=apply,
                load_row=load,
            )
        )

    await mutate_many_with_event(session, items=specs)
    return new_ids
