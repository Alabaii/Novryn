"""Интеграция (Фаза 2, план 03 / TASK-10): bulk create_subtasks атомарен.

- success: create_subtasks(parent, 3) → 3 задачи с parent_task_id==parent, ровно
  3 события task.created (по одному на ребёнка, D-02); функция вернула 3 id;
- all-or-nothing: батч с одной невалидной подзадачей (status нарушает CHECK) →
  create_subtasks падает; НИ одной подзадачи и НИ одного события не закоммичено (D-03).

Свежая AsyncSession на операцию (Pitfall 3); зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.models import Event, Task
from novryn.domain.events import ActorType, EventType
from novryn.services import task_service


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _new_task(maker: async_sessionmaker[AsyncSession], title: str) -> uuid.UUID:
    async with maker() as s:
        return await task_service.create_task(
            s, title=title, actor_type=ActorType.USER, actor_id=None, status="TODO"
        )


@pytest.mark.asyncio
async def test_create_subtasks_creates_n_children_with_events(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parent = await _new_task(uow_sessionmaker, "parent")
    async with uow_sessionmaker() as s:
        ids = await task_service.create_subtasks(
            s,
            parent_task_id=parent,
            subtasks=[{"title": f"sub {i}", "status": "TODO"} for i in range(3)],
            actor_type=ActorType.USER,
            actor_id=None,
        )
    assert len(ids) == 3

    async with uow_sessionmaker() as check:
        children = (
            await check.execute(select(Task).where(Task.parent_task_id == parent))
        ).scalars().all()
        assert len(children) == 3
        assert all(c.parent_task_id == parent for c in children)

        events = (
            await check.execute(select(Event).where(Event.entity_id.in_(ids)))
        ).scalars().all()
        assert len(events) == 3  # один task.created на ребёнка (D-02)
        assert all(e.event_type == EventType.TASK_CREATED for e in events)


@pytest.mark.asyncio
async def test_create_subtasks_all_or_nothing(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    parent = await _new_task(uow_sessionmaker, "parent-rollback")
    with pytest.raises(IntegrityError):
        async with uow_sessionmaker() as s:
            await task_service.create_subtasks(
                s,
                parent_task_id=parent,
                subtasks=[
                    {"title": "ok", "status": "TODO"},
                    {"title": "bad", "status": "BOGUS"},  # нарушает ck_tasks_status
                    {"title": "never", "status": "TODO"},
                ],
                actor_type=ActorType.USER,
                actor_id=None,
            )

    async with uow_sessionmaker() as check:
        child_count = await check.scalar(
            select(text("count(*)")).select_from(Task).where(Task.parent_task_id == parent)
        )
        event_count = await check.scalar(
            text(
                "SELECT count(*) FROM events "
                "WHERE payload_json->'after'->>'parent_task_id' = :p"
            ),
            {"p": str(parent)},
        )
        assert child_count == 0  # ни одной подзадачи (D-03)
        assert event_count == 0  # ни одного orphan-события
