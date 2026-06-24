"""Интеграция (Фаза 2, план 02 / TASK-10): батч-UoW mutate_many_with_event атомарен.

- success: батч из 3 мутаций → РОВНО 3 task И 3 события ``task.created`` (D-02 —
  один event на элемент), у каждого before=None / after=строка;
- rollback: исключение на 2-м элементе → ВЕСЬ батч откатывается, 0 task и 0
  событий (D-03 all-or-nothing — даже 1-й, уже применённый, элемент откатан).

Свежая AsyncSession на каждую логическую операцию (Pitfall 3); зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Task
from novryn.domain.events import ActorType, EventType
from novryn.repositories.uow import MutationSpec, mutate_many_with_event


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх мигрированной БД (UoW открывает begin() сам)."""
    return sessionmaker


async def _load_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


def _insert_spec(task_id: uuid.UUID, title: str) -> MutationSpec:
    """Спецификация: вставить одну Task, событие task.created (before=None)."""

    async def apply(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(id=task_id, title=title, status="TODO")
        )
        return task_id

    return MutationSpec(
        entity_type="task",
        entity_id=task_id,
        event_type=EventType.TASK_CREATED,
        actor_type=ActorType.USER,
        actor_id=None,
        apply=apply,
        load_row=lambda s: _load_task(s, task_id),
    )


@pytest.mark.asyncio
async def test_batch_writes_n_events_atomically(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Батч из 3 → 3 task + 3 события task.created, before=None / after=строка."""
    ids = [new_id(), new_id(), new_id()]
    items = [_insert_spec(tid, f"subtask {i}") for i, tid in enumerate(ids)]

    async with uow_sessionmaker() as session:
        results = await mutate_many_with_event(session, items=items)
    assert results == ids  # порядок результатов сохранён

    async with uow_sessionmaker() as check:
        task_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.id.in_(ids))
        )
        assert task_count == 3

        events = (
            await check.execute(select(Event).where(Event.entity_id.in_(ids)))
        ).scalars().all()
        assert len(events) == 3  # ровно один event на элемент (D-02)
        assert all(e.event_type == EventType.TASK_CREATED for e in events)
        for e in events:
            assert e.payload_json["before"] is None  # CREATE → before=null
            after = cast(dict[str, Any], e.payload_json["after"])
            assert after["id"] == str(e.entity_id)  # after — строка этого элемента


@pytest.mark.asyncio
async def test_batch_rolls_back_entirely_on_failure(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Сбой на 2-м элементе откатывает ВЕСЬ батч (D-03): 0 task, 0 событий."""
    ids = [new_id(), new_id(), new_id()]
    good1 = _insert_spec(ids[0], "ok-1")

    async def boom(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(id=ids[1], title="boom", status="TODO")
        )
        raise RuntimeError("boom on item 2")

    bad = MutationSpec(
        entity_type="task",
        entity_id=ids[1],
        event_type=EventType.TASK_CREATED,
        actor_type=ActorType.USER,
        actor_id=None,
        apply=boom,
        load_row=lambda s: _load_task(s, ids[1]),
    )
    good3 = _insert_spec(ids[2], "ok-3")

    with pytest.raises(RuntimeError, match="boom on item 2"):
        async with uow_sessionmaker() as session:
            await mutate_many_with_event(session, items=[good1, bad, good3])

    async with uow_sessionmaker() as check:
        task_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.id.in_(ids))
        )
        event_count = await check.scalar(
            select(text("count(*)")).select_from(Event).where(Event.entity_id.in_(ids))
        )
        assert task_count == 0  # даже уже применённый good1 откатился
        assert event_count == 0  # никаких orphan-событий
