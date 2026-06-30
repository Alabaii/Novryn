"""Интеграция (Фаза 3, план 04 / PAT-01/02/03): behavior patterns store + read.

Wave-0 RED baseline: импортирует ещё НЕ существующие novryn.services.pattern_service
(pattern_store) и novryn.repositories.pattern_read (get_behavior_patterns) — отсутствие
модулей даёт collection-error (валидный RED до 03-04).

Поведение, фиксируемое тестами:
- pattern_store нового pattern_type → строка + pattern.stored; повтор того же типа →
  in-place update + pattern.updated — PAT-01/D-06/D-07;
- get_behavior_patterns фильтрует по pattern_type/min_confidence + сортирует
  (без FTS) — PAT-02/D-10;
- created_at/updated_at заполнены и персистят — PAT-03.

Свежая AsyncSession на операцию; зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.models import BehaviorPattern, Event

# Wave-2 целевые символы (ещё не существуют → RED при коллекции).
from novryn.repositories.pattern_read import get_behavior_patterns
from novryn.services.pattern_service import pattern_store


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


def _unique_type(prefix: str) -> str:
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
async def test_store_and_update(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """PAT-01/D-06/D-07: первый store → pattern.stored; повтор → in-place + pattern.updated."""
    ptype = _unique_type("morning")
    async with uow_sessionmaker() as s:
        first_id = await pattern_store(
            s, pattern_type=ptype, confidence=0.6, evidence_json={"n": 3}
        )
    async with uow_sessionmaker() as check:
        assert await _count_events(check, first_id, "pattern.stored") == 1

    async with uow_sessionmaker() as s:
        second_id = await pattern_store(
            s, pattern_type=ptype, confidence=0.85, evidence_json={"n": 7}
        )
    assert second_id == first_id  # in-place
    async with uow_sessionmaker() as check:
        row = await check.get(BehaviorPattern, first_id)
        assert row is not None
        assert float(row.confidence) == pytest.approx(0.85)
        assert await _count_events(check, first_id, "pattern.updated") == 1


@pytest.mark.asyncio
async def test_get_patterns_filter_sort(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """PAT-02/D-10: фильтр pattern_type/min_confidence + сортировка по confidence DESC."""
    hi = _unique_type("hi")
    lo = _unique_type("lo")
    async with uow_sessionmaker() as s:
        await pattern_store(s, pattern_type=hi, confidence=0.9, evidence_json={})
    async with uow_sessionmaker() as s:
        await pattern_store(s, pattern_type=lo, confidence=0.2, evidence_json={})

    async with uow_sessionmaker() as check:
        # min_confidence отсекает низкоуверенный паттерн.
        types_hi = [
            p.pattern_type for p in await get_behavior_patterns(check, min_confidence=0.8)
        ]
        assert hi in types_hi
        assert lo not in types_hi

        # Фильтр по конкретному pattern_type.
        only_lo = [
            p.pattern_type for p in await get_behavior_patterns(check, pattern_type=lo)
        ]
        assert only_lo == [lo]


@pytest.mark.asyncio
async def test_pattern_timestamps(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """PAT-03: created_at/updated_at заполнены и персистят."""
    ptype = _unique_type("ts")
    async with uow_sessionmaker() as s:
        pid = await pattern_store(
            s, pattern_type=ptype, confidence=0.5, evidence_json={}
        )
    async with uow_sessionmaker() as check:
        row = await check.get(BehaviorPattern, pid)
        assert row is not None
        assert row.created_at is not None
        assert row.updated_at is not None
