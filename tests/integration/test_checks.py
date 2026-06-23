"""Интеграция (defense-in-depth, EVT-02 + V5): CHECK-ограничения отвергают мусор.

По одному `pytest.raises` на нарушение каждого CHECK:
- tasks.status вне канона;
- tasks.energy_required вне {LOW,MEDIUM,HIGH};
- sessions.result вне канона;
- attachments.type вне канона;
- user_memory.confidence = 1.5 (вне [0,1]);
- events.actor_type = 'ADMIN' (вне {USER,HERMES,SYSTEM}).

Свежая сессия на тест; зависит от `migrated_db`. Каждый INSERT с нарушением
откатывается, чтобы не загрязнять сессию.
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


async def _expect_check_violation(
    session: AsyncSession, sql: str, params: dict[str, object]
) -> None:
    # CHECK срабатывает на этапе выполнения INSERT, до commit.
    with pytest.raises(IntegrityError):
        await session.execute(text(sql), params)
    await session.rollback()


@pytest.mark.asyncio
async def test_task_status_out_of_set_rejected(msession: AsyncSession) -> None:
    await _expect_check_violation(
        msession,
        "INSERT INTO tasks (id, title, status) VALUES (:id, 't', 'NONSENSE')",
        {"id": new_id()},
    )


@pytest.mark.asyncio
async def test_task_energy_out_of_set_rejected(msession: AsyncSession) -> None:
    await _expect_check_violation(
        msession,
        "INSERT INTO tasks (id, title, status, energy_required) "
        "VALUES (:id, 't', 'TODO', 'EXTREME')",
        {"id": new_id()},
    )


@pytest.mark.asyncio
async def test_session_result_out_of_set_rejected(msession: AsyncSession) -> None:
    task_id = new_id()
    await msession.execute(
        text("INSERT INTO tasks (id, title, status) VALUES (:id, 't', 'TODO')"),
        {"id": task_id},
    )
    await msession.commit()
    await _expect_check_violation(
        msession,
        "INSERT INTO sessions (id, task_id, result) VALUES (:id, :tid, 'MAYBE')",
        {"id": new_id(), "tid": task_id},
    )


@pytest.mark.asyncio
async def test_attachment_type_out_of_set_rejected(msession: AsyncSession) -> None:
    task_id = new_id()
    await msession.execute(
        text("INSERT INTO tasks (id, title, status) VALUES (:id, 't', 'TODO')"),
        {"id": task_id},
    )
    await msession.commit()
    await _expect_check_violation(
        msession,
        "INSERT INTO attachments (id, task_id, type) VALUES (:id, :tid, 'TELEPATHY')",
        {"id": new_id(), "tid": task_id},
    )


@pytest.mark.asyncio
async def test_user_memory_confidence_out_of_range_rejected(
    msession: AsyncSession,
) -> None:
    await _expect_check_violation(
        msession,
        "INSERT INTO user_memory (id, confidence) VALUES (:id, 1.5)",
        {"id": new_id()},
    )


@pytest.mark.asyncio
async def test_event_actor_type_out_of_set_rejected(msession: AsyncSession) -> None:
    await _expect_check_violation(
        msession,
        "INSERT INTO events (id, event_type, entity_type, entity_id, actor_type) "
        "VALUES (:id, 'e', 'task', :eid, 'ADMIN')",
        {"id": new_id(), "eid": new_id()},
    )
