"""Интеграция (Фаза 2, план 03 / TASK-07,08): lifecycle complete/archive/block/unblock.

- complete_task → status=DONE + completed_at, ровно одно task.completed;
- archive_task → status=ARCHIVED + archived_at, ровно одно task.archived;
- block(reason) → BLOCKED + blocked_reason; unblock → TODO + blocked_reason=None;
  события task.blocked / task.unblocked;
- последовательность событий = бизнес-операции (D-01): created→completed→archived;
- D-09 (no-cascade): archive задачи с активным вложением и активной зависимостью
  оставляет обе связи активными (deleted_at IS NULL) — lifecycle НЕ каскадит.

Свежая AsyncSession на операцию (Pitfall 3); зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Attachment, Event, Task, TaskDependency
from novryn.domain.events import ActorType, EventType
from novryn.services import task_service


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _event_types(check: AsyncSession, task_id: uuid.UUID) -> list[str]:
    rows = (
        await check.execute(
            select(Event).where(Event.entity_id == task_id).order_by(Event.occurred_at)
        )
    ).scalars().all()
    return [e.event_type for e in rows]


async def _new_task(maker: async_sessionmaker[AsyncSession], title: str) -> uuid.UUID:
    async with maker() as s:
        return await task_service.create_task(
            s, title=title, actor_type=ActorType.USER, actor_id=None, status="TODO"
        )


@pytest.mark.asyncio
async def test_complete_sets_done_and_writes_event(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _new_task(uow_sessionmaker, "to complete")
    async with uow_sessionmaker() as s:
        await task_service.complete_task(
            s, task_id=task_id, actor_type=ActorType.HERMES, actor_id=None
        )
    async with uow_sessionmaker() as check:
        task = await check.get(Task, task_id)
        assert task is not None
        assert task.status == "DONE"
        assert task.completed_at is not None
        assert await _event_types(check, task_id) == [
            EventType.TASK_CREATED,
            EventType.TASK_COMPLETED,
        ]


@pytest.mark.asyncio
async def test_archive_sets_archived_and_writes_event(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _new_task(uow_sessionmaker, "to archive")
    async with uow_sessionmaker() as s:
        await task_service.archive_task(
            s, task_id=task_id, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        task = await check.get(Task, task_id)
        assert task is not None
        assert task.status == "ARCHIVED"
        assert task.archived_at is not None
        assert EventType.TASK_ARCHIVED in await _event_types(check, task_id)


@pytest.mark.asyncio
async def test_block_then_unblock(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _new_task(uow_sessionmaker, "to block")
    async with uow_sessionmaker() as s:
        await task_service.block(
            s, task_id=task_id, reason="ждёт ревью", actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        task = await check.get(Task, task_id)
        assert task is not None
        assert task.status == "BLOCKED"
        assert task.blocked_reason == "ждёт ревью"

    async with uow_sessionmaker() as s:
        await task_service.unblock(
            s, task_id=task_id, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        task = await check.get(Task, task_id)
        assert task is not None
        assert task.status == "TODO"
        assert task.blocked_reason is None
        assert await _event_types(check, task_id) == [
            EventType.TASK_CREATED,
            EventType.TASK_BLOCKED,
            EventType.TASK_UNBLOCKED,
        ]


@pytest.mark.asyncio
async def test_complete_then_archive_event_order(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D-01: каждая операция — своё событие; порядок created→completed→archived."""
    task_id = await _new_task(uow_sessionmaker, "lifecycle")
    async with uow_sessionmaker() as s:
        await task_service.complete_task(
            s, task_id=task_id, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as s:
        await task_service.archive_task(
            s, task_id=task_id, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        assert await _event_types(check, task_id) == [
            EventType.TASK_CREATED,
            EventType.TASK_COMPLETED,
            EventType.TASK_ARCHIVED,
        ]


@pytest.mark.asyncio
async def test_archive_does_not_cascade_to_links(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D-09: archive НЕ трогает активные attachment/dependency (Novryn хранит факты)."""
    task_id = await _new_task(uow_sessionmaker, "with links")
    dep_target = await _new_task(uow_sessionmaker, "dep target")
    att_id = new_id()
    dep_id = new_id()
    # Активные связи (прямые insert'ы — это setup теста, не доменная операция).
    async with uow_sessionmaker() as s:
        async with s.begin():
            await s.execute(
                insert(Attachment).values(
                    id=att_id, task_id=task_id, type="URL", url="http://example"
                )
            )
            await s.execute(
                insert(TaskDependency).values(
                    id=dep_id, task_id=task_id, depends_on_task_id=dep_target
                )
            )

    async with uow_sessionmaker() as s:
        await task_service.archive_task(
            s, task_id=task_id, actor_type=ActorType.USER, actor_id=None
        )

    async with uow_sessionmaker() as check:
        att = await check.get(Attachment, att_id)
        dep = await check.get(TaskDependency, dep_id)
        assert att is not None and att.deleted_at is None  # связь осталась активной
        assert dep is not None and dep.deleted_at is None
