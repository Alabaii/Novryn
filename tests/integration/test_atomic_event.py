"""Интеграция (критерий #3 / EVT-01 / NFR-06): мутация и событие атомарны.

Доказывает структурный аудит-инвариант на реальном PostgreSQL 16:
- happy: мутация через UoW даёт РОВНО одну строку сущности И ровно одно событие;
  payload.before=null (CREATE), payload.after=полная строка task; поля события
  (event_type/entity_type/entity_id/actor_type) — те, что передал сервис (D-05);
- rollback A (мутация→событие): исключение ВНУТРИ транзакции после доменного
  INSERT, до записи события → после отката нет НИ строки task, НИ события;
- rollback B (событие→мутация): сбой самого INSERT события (невалидный
  actor_type → нарушение ck_events_actor_type) → доменная мутация тоже
  откатывается, строки task нет.

Записать мутацию без события (или событие без мутации) невозможно по дизайну:
обе записи в одном ``session.begin()`` (D-04). Свежая AsyncSession на каждую
логическую операцию (Pitfall 3); зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Task
from novryn.domain.events import ActorType, EventType
from novryn.repositories.uow import mutate_with_event


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх мигрированной БД.

    UoW открывает ``session.begin()`` сам, поэтому тестам нужна именно ФАБРИКА:
    каждая логическая операция (мутация, затем независимая проверка) берёт свежую
    короткоживущую сессию (Anti-Pattern 5 / Pitfall 3).
    """
    return sessionmaker


async def _load_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Загрузить полную строку task по id (снимок before/after для UoW)."""
    return await session.get(Task, task_id)


@pytest.mark.asyncio
async def test_create_task_writes_exactly_one_event_atomically(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: ровно одна task И ровно одно событие; before=null, after=строка."""
    task_id = new_id()
    actor_id = new_id()

    async def apply(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(
                id=task_id,
                title="Написать тест атомарности",
                status="TODO",
                ai_context_json={"hermes_note": "high priority", "score": 7},
            )
        )
        return task_id

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_CREATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            apply=apply,
            load_row=lambda s: _load_task(s, task_id),
        )

    # Проверка в НОВОЙ сессии — данные действительно закоммичены.
    async with uow_sessionmaker() as check:
        task_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.id == task_id)
        )
        assert task_count == 1

        events = (
            await check.execute(select(Event).where(Event.entity_id == task_id))
        ).scalars().all()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == EventType.TASK_CREATED
        assert event.entity_type == "task"
        assert event.entity_id == task_id
        assert event.actor_type == ActorType.USER
        assert event.actor_id == actor_id
        assert event.schema_version == 1

        payload = event.payload_json
        assert payload["before"] is None  # CREATE → before=null (D-01/D-02)
        after = cast(dict[str, Any], payload["after"])
        assert after is not None
        # after — полная строка task (D-02): ключевые поля присутствуют.
        assert after["id"] == str(task_id)
        assert after["title"] == "Написать тест атомарности"
        assert after["status"] == "TODO"
        # ai_context_json включён целиком (D-03).
        assert after["ai_context_json"] == {"hermes_note": "high priority", "score": 7}
        # server-default created_at материализован до снимка after.
        assert after["created_at"] is not None


@pytest.mark.asyncio
async def test_rollback_mutation_then_event_leaves_neither(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Rollback A: исключение после доменного INSERT, до события → нет ни того, ни другого."""
    task_id = new_id()

    async def apply(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(id=task_id, title="boom", status="TODO")
        )
        # Сбой ВНУТРИ транзакции после доменной записи, ДО записи события.
        raise RuntimeError("boom before event insert")

    with pytest.raises(RuntimeError, match="boom before event insert"):
        async with uow_sessionmaker() as session:
            await mutate_with_event(
                session,
                entity_type="task",
                entity_id=task_id,
                event_type=EventType.TASK_CREATED,
                actor_type=ActorType.USER,
                actor_id=None,
                apply=apply,
                load_row=lambda s: _load_task(s, task_id),
            )

    # В НОВОЙ сессии: ни строки task, ни события (вся транзакция откатилась).
    async with uow_sessionmaker() as check:
        task_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.id == task_id)
        )
        event_count = await check.scalar(
            select(text("count(*)")).select_from(Event).where(Event.entity_id == task_id)
        )
        assert task_count == 0
        assert event_count == 0


@pytest.mark.asyncio
async def test_rollback_event_failure_rolls_back_mutation(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Rollback B: сбой INSERT события (невалидный actor_type) откатывает мутацию."""
    task_id = new_id()

    async def apply(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(id=task_id, title="domain ok", status="TODO")
        )
        return task_id

    # actor_type='ADMIN' нарушает ck_events_actor_type → INSERT события падает,
    # вся транзакция (вместе с доменным INSERT) откатывается.
    with pytest.raises(IntegrityError):
        async with uow_sessionmaker() as session:
            await mutate_with_event(
                session,
                entity_type="task",
                entity_id=task_id,
                event_type=EventType.TASK_CREATED,
                actor_type="ADMIN",  # невалидно
                actor_id=None,
                apply=apply,
                load_row=lambda s: _load_task(s, task_id),
            )

    async with uow_sessionmaker() as check:
        task_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.id == task_id)
        )
        event_count = await check.scalar(
            select(text("count(*)")).select_from(Event).where(Event.entity_id == task_id)
        )
        # Доменная строка тоже отсутствует — одно без другого невозможно.
        assert task_count == 0
        assert event_count == 0


@pytest.mark.asyncio
async def test_update_through_uow_is_also_atomic(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """UPDATE через UoW пишет ровно одно доп. событие в той же транзакции."""
    task_id = new_id()

    async def create(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(id=task_id, title="v1", status="TODO")
        )
        return task_id

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_CREATED,
            actor_type=ActorType.USER,
            actor_id=None,
            apply=create,
            load_row=lambda s: _load_task(s, task_id),
        )

    async def complete(session: AsyncSession) -> None:
        await session.execute(
            update(Task).where(Task.id == task_id).values(status="DONE")
        )

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_COMPLETED,
            actor_type=ActorType.HERMES,
            actor_id=None,
            apply=complete,
            load_row=lambda s: _load_task(s, task_id),
        )

    async with uow_sessionmaker() as check:
        events = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == task_id)
                .order_by(Event.occurred_at)
            )
        ).scalars().all()
        # Ровно два события: created + completed.
        assert [e.event_type for e in events] == [
            EventType.TASK_CREATED,
            EventType.TASK_COMPLETED,
        ]
