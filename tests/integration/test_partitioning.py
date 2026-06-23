"""Интеграция (критерий #2 / EVT-05): events партиционирована по occurred_at.

Проверяем:
- events зарегистрирована в pg_partitioned_table (range-партиционирование);
- INSERT строки с occurred_at внутри известного месяца физически попадает в
  соответствующую месячную партицию (через tableoid::regclass);
- INSERT с occurred_at далеко за 12-месячным горизонтом попадает в events_default,
  и сам INSERT НЕ падает (D-10 страховка).

Свежая сессия на тест; зависит от `migrated_db`.
"""

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


async def _insert_event(
    session: AsyncSession, occurred_at: datetime.datetime
) -> uuid.UUID:
    event_id = new_id()
    await session.execute(
        text(
            "INSERT INTO events "
            "(id, occurred_at, event_type, entity_type, entity_id, actor_type) "
            "VALUES (:id, :ts, 'test.created', 'task', :eid, 'SYSTEM')"
        ),
        {"id": event_id, "ts": occurred_at, "eid": new_id()},
    )
    await session.commit()
    return event_id


async def _partition_of(session: AsyncSession, event_id: uuid.UUID) -> str:
    row = await session.execute(
        text("SELECT tableoid::regclass::text FROM events WHERE id = :id"),
        {"id": event_id},
    )
    return str(row.scalar_one())


@pytest.mark.asyncio
async def test_events_is_range_partitioned(msession: AsyncSession) -> None:
    row = await msession.execute(
        text(
            # partstrat — тип "char"; приводим к text, чтобы asyncpg отдал str, а
            # не bytes (b'r'). 'r' = RANGE-партиционирование.
            "SELECT partstrat::text FROM pg_partitioned_table p "
            "JOIN pg_class c ON c.oid = p.partrelid "
            "WHERE c.relname = 'events'"
        )
    )
    assert row.scalar_one() == "r"


@pytest.mark.asyncio
async def test_insert_lands_in_correct_monthly_partition(
    msession: AsyncSession,
) -> None:
    # 2026-07 — внутри горизонта; ожидаем партицию events_2026_07.
    ts = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)
    event_id = await _insert_event(msession, ts)
    partition = await _partition_of(msession, event_id)
    assert partition == "events_2026_07", partition


@pytest.mark.asyncio
async def test_out_of_horizon_insert_lands_in_default(
    msession: AsyncSession,
) -> None:
    # 2099 — заведомо за горизонтом 12 месяцев; должно попасть в events_default,
    # а INSERT не должен упасть (D-10).
    ts = datetime.datetime(2099, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    event_id = await _insert_event(msession, ts)
    partition = await _partition_of(msession, event_id)
    assert partition == "events_default", partition
