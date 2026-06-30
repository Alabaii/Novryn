"""Интеграция (Фаза 3, план 03 / MEM-01/02/04): user memory store + upsert.

Wave-0 RED baseline: импортирует ещё НЕ существующий novryn.services.memory_service
(memory_store) — отсутствие модуля даёт collection-error (валидный RED до 03-03).

Поведение, фиксируемое тестами:
- memory_store нового memory_type создаёт строку + пишет memory.stored — MEM-01/D-07;
- memory_store того же memory_type обновляет строку in-place + пишет memory.updated — D-05;
- confidence вне 0.0–1.0 отвергается на write IntegrityError (НЕ ValueError — Pitfall 4) — MEM-02;
- updated_at реально продвигается на апдейте (updated_at > created_at — Pitfall 3) — MEM-04.

Свежая AsyncSession на операцию (Pitfall 3); зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.models import Event, UserMemory

# Wave-2 целевой символ (ещё не существует → RED при коллекции).
from novryn.services.memory_service import memory_store


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


def _unique_type(prefix: str) -> str:
    """Уникальный memory_type на тест (UNIQUE-ключ upsert, общая session-scoped БД)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _count_events(s: AsyncSession, entity_id: uuid.UUID, event_type: str) -> int:
    n = await s.scalar(
        select(text("count(*)"))
        .select_from(Event)
        .where(Event.entity_id == entity_id)
        .where(Event.event_type == event_type)
    )
    return int(n or 0)


@pytest.mark.asyncio
async def test_store_creates(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MEM-01/D-07: новый memory_type → строка + memory.stored."""
    mtype = _unique_type("pref")
    async with uow_sessionmaker() as s:
        mem_id = await memory_store(
            s, memory_type=mtype, content="любит утро", confidence=0.75, source="hermes"
        )
    async with uow_sessionmaker() as check:
        row = await check.get(UserMemory, mem_id)
        assert row is not None
        assert row.memory_type == mtype
        assert await _count_events(check, mem_id, "memory.stored") == 1


@pytest.mark.asyncio
async def test_store_same_type_updates_inplace_event_updated(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D-05: повторный store того же типа — in-place update + memory.updated."""
    mtype = _unique_type("inplace")
    async with uow_sessionmaker() as s:
        first_id = await memory_store(
            s, memory_type=mtype, content="старое", confidence=0.5, source="hermes"
        )
    async with uow_sessionmaker() as s:
        second_id = await memory_store(
            s, memory_type=mtype, content="новое", confidence=0.9, source="hermes"
        )
    # Та же строка (in-place, не новая).
    assert second_id == first_id
    async with uow_sessionmaker() as check:
        row = await check.get(UserMemory, first_id)
        assert row is not None
        assert row.content == "новое"
        assert await _count_events(check, first_id, "memory.updated") == 1


@pytest.mark.asyncio
async def test_confidence_out_of_range_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MEM-02/Pitfall 4: confidence вне 0.0–1.0 → IntegrityError (БД CHECK), не ValueError."""
    mtype = _unique_type("badconf")
    with pytest.raises(IntegrityError):
        async with uow_sessionmaker() as s:
            await memory_store(
                s, memory_type=mtype, content="x", confidence=1.5, source="hermes"
            )


@pytest.mark.asyncio
async def test_updated_at_changes_on_update(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MEM-04/Pitfall 3: updated_at реально продвигается на апдейте."""
    mtype = _unique_type("ts")
    async with uow_sessionmaker() as s:
        mem_id = await memory_store(
            s, memory_type=mtype, content="v1", confidence=0.4, source="hermes"
        )
    async with uow_sessionmaker() as check:
        before = await check.get(UserMemory, mem_id)
        assert before is not None
        created = before.created_at

    await asyncio.sleep(0.01)
    async with uow_sessionmaker() as s:
        await memory_store(
            s, memory_type=mtype, content="v2", confidence=0.6, source="hermes"
        )
    async with uow_sessionmaker() as check:
        after = await check.get(UserMemory, mem_id)
        assert after is not None
        assert after.updated_at is not None
        assert after.updated_at > created
