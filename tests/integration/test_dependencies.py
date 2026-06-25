"""Интеграция (Фаза 2, план 05 / DEP-01..05): link/unlink + цикл/self в транзакции.

- link_tasks → одно dependency.linked; get_dependencies отдаёт активное ребро;
- self-dependency → SelfDependencyError, без строки/события (DEP-04);
- цикл → CycleError, БЕЗ новой строки и БЕЗ события (проверка внутри транзакции, DEP-05);
- unlink → soft-delete + dependency.unlinked; re-link → НОВАЯ строка + событие, старая
  soft-deleted остаётся (D-07); unlink отсутствующего ребра — no-op (D-08).

Свежая AsyncSession на операцию; зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Task, TaskDependency
from novryn.domain.errors import CycleError, SelfDependencyError
from novryn.domain.events import ActorType, EventType
from novryn.repositories import dependency_read
from novryn.services import dependency_service


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _task(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = new_id()
    async with maker() as s:
        async with s.begin():
            await s.execute(insert(Task).values(id=tid, title="t", status="TODO"))
    return tid


async def _count_linked_events(check: AsyncSession) -> int:
    n = await check.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.event_type == EventType.DEPENDENCY_LINKED)
    )
    return int(n or 0)


@pytest.mark.asyncio
async def test_link_and_get_dependencies(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await _task(uow_sessionmaker), await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        dep_id = await dependency_service.link_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        edges = await dependency_read.get_dependencies(check, a)
        assert [e.depends_on_task_id for e in edges] == [b]
        events = (
            await check.execute(select(Event).where(Event.entity_id == dep_id))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == EventType.DEPENDENCY_LINKED


@pytest.mark.asyncio
async def test_self_dep_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a = await _task(uow_sessionmaker)
    before = await _count_linked_events_in(uow_sessionmaker)
    with pytest.raises(SelfDependencyError):
        async with uow_sessionmaker() as s:
            await dependency_service.link_tasks(
                s, task_id=a, depends_on_task_id=a, actor_type=ActorType.USER, actor_id=None
            )
    async with uow_sessionmaker() as check:
        assert await dependency_read.get_dependencies(check, a) == []
        assert await _count_linked_events(check) == before  # событий не добавилось


@pytest.mark.asyncio
async def test_cycle_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """a→b существует; b→a замкнул бы цикл → CycleError, без строки и без события."""
    a, b = await _task(uow_sessionmaker), await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        await dependency_service.link_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    before = await _count_linked_events_in(uow_sessionmaker)
    with pytest.raises(CycleError):
        async with uow_sessionmaker() as s:
            await dependency_service.link_tasks(
                s, task_id=b, depends_on_task_id=a, actor_type=ActorType.USER, actor_id=None
            )
    async with uow_sessionmaker() as check:
        # Никакого ребра b→a (откат внутри транзакции).
        rows = (
            await check.execute(
                select(TaskDependency).where(TaskDependency.task_id == b)
            )
        ).scalars().all()
        assert rows == []
        assert await _count_linked_events(check) == before  # событие не записано


@pytest.mark.asyncio
async def test_unlink_relink(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await _task(uow_sessionmaker), await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        first_id = await dependency_service.link_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    # unlink → soft-delete + событие
    async with uow_sessionmaker() as s:
        unlinked = await dependency_service.unlink_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    assert unlinked == first_id
    async with uow_sessionmaker() as check:
        assert await dependency_read.get_dependencies(check, a) == []  # активного нет
        old = await check.get(TaskDependency, first_id)
        assert old is not None and old.deleted_at is not None  # история сохранена (D-07)
        unlinked_events = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == first_id)
                .where(Event.event_type == EventType.DEPENDENCY_UNLINKED)
            )
        ).scalars().all()
        assert len(unlinked_events) == 1

    # re-link той же пары → НОВАЯ строка + новое событие
    async with uow_sessionmaker() as s:
        second_id = await dependency_service.link_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    assert second_id != first_id  # новый id (D-07)
    async with uow_sessionmaker() as check:
        active = await dependency_read.get_dependencies(check, a)
        assert [e.id for e in active] == [second_id]


@pytest.mark.asyncio
async def test_unlink_missing_is_noop(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await _task(uow_sessionmaker), await _task(uow_sessionmaker)
    before = await _count_unlinked_events_in(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        result = await dependency_service.unlink_tasks(
            s, task_id=a, depends_on_task_id=b, actor_type=ActorType.USER, actor_id=None
        )
    assert result is None  # ребра не было — no-op
    after = await _count_unlinked_events_in(uow_sessionmaker)
    assert after == before  # события не записано (D-08)


async def _count_linked_events_in(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as check:
        return await _count_linked_events(check)


async def _count_unlinked_events_in(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as check:
        n = await check.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == EventType.DEPENDENCY_UNLINKED)
        )
        return int(n or 0)
