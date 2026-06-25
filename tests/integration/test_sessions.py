"""Интеграция (Фаза 2, план 05 / SESS-01..04): start/finish/history сессий.

- start_session → planned_minutes + started_at + session.started; статус задачи НЕ меняется (A4);
- finish_session → actual_minutes + ended_at + result + notes + session.ended;
  невалидный result отклонён ck_sessions_result (SESS-03);
- две start_session на одну задачу — обе успешны (A5, несколько открытых сессий);
- get_sessions — все сессии задачи по started_at, id ASC (SESS-04/A7).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Session, Task
from novryn.domain.events import ActorType, EventType
from novryn.repositories import dependency_read
from novryn.services import session_service


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


@pytest.mark.asyncio
async def test_start_session_records_fact_without_status_change(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        sess_id = await session_service.start_session(
            s, task_id=task_id, planned_minutes=25, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        sess = await check.get(Session, sess_id)
        assert sess is not None
        assert sess.planned_minutes == 25
        assert sess.started_at is not None
        task = await check.get(Task, task_id)
        assert task is not None and task.status == "TODO"  # A4: статус не тронут
        events = (
            await check.execute(select(Event).where(Event.entity_id == sess_id))
        ).scalars().all()
        assert [e.event_type for e in events] == [EventType.SESSION_STARTED]


@pytest.mark.asyncio
async def test_finish_session_records_outcome(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        sess_id = await session_service.start_session(
            s, task_id=task_id, planned_minutes=25, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as s:
        await session_service.finish_session(
            s,
            session_id=sess_id,
            actual_minutes=30,
            result="COMPLETED",
            notes="прошло хорошо",
            actor_type=ActorType.USER,
            actor_id=None,
        )
    async with uow_sessionmaker() as check:
        sess = await check.get(Session, sess_id)
        assert sess is not None
        assert sess.actual_minutes == 30
        assert sess.ended_at is not None
        assert sess.result == "COMPLETED"
        assert sess.notes == "прошло хорошо"
        types = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == sess_id)
                .order_by(Event.occurred_at)
            )
        ).scalars().all()
        assert [e.event_type for e in types] == [
            EventType.SESSION_STARTED,
            EventType.SESSION_ENDED,
        ]


@pytest.mark.asyncio
async def test_finish_invalid_result_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        sess_id = await session_service.start_session(
            s, task_id=task_id, planned_minutes=10, actor_type=ActorType.USER, actor_id=None
        )
    with pytest.raises(IntegrityError):
        async with uow_sessionmaker() as s:
            await session_service.finish_session(
                s,
                session_id=sess_id,
                actual_minutes=10,
                result="BOGUS",  # нарушает ck_sessions_result
                actor_type=ActorType.USER,
                actor_id=None,
            )


@pytest.mark.asyncio
async def test_multiple_open_sessions_and_order(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Две открытые сессии (A5) + get_sessions по started_at/id ASC (A7)."""
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        s1 = await session_service.start_session(
            s, task_id=task_id, planned_minutes=15, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as s:
        s2 = await session_service.start_session(
            s, task_id=task_id, planned_minutes=20, actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as check:
        sessions = await dependency_read.get_sessions(check, task_id)
        assert [s.id for s in sessions] == [s1, s2]  # обе есть, порядок ASC
