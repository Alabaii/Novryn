"""Интеграция (Фаза 4, план 01 / PERM-04): автор события задаётся вызывающим.

Wave-0 RED baseline. До 04-01 ``generate_daily_focus`` жёстко писал
``actor_type=HERMES, actor_id=None``, а ``memory_store``/``pattern_store`` молча
подставляли ``HERMES`` по умолчанию. MCP-слой Фазы 4 берёт автора из токена, поэтому
сервисы обязаны принимать его ЯВНО и записывать в событие как есть.

Поведение, фиксируемое тестами:
- focus.generated несёт actor_type/actor_id, переданные вызывающим;
- memory_store/pattern_store без actor — TypeError на вызове (умолчания нет);
- memory.stored/pattern.stored несут actor_type/actor_id, переданные вызывающим.

Свежая AsyncSession на операцию (Pitfall 3); зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event
from novryn.domain.events import ActorType
from novryn.services.focus_service import generate_daily_focus
from novryn.services.memory_service import memory_store
from novryn.services.pattern_service import pattern_store


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


def _unique_type(prefix: str) -> str:
    """Уникальный memory_type/pattern_type на тест (UNIQUE-ключ upsert, общая БД)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _single_event(check: AsyncSession, entity_id: uuid.UUID) -> Event:
    return (
        await check.execute(select(Event).where(Event.entity_id == entity_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_focus_event_records_caller_actor(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """focus.generated несёт автора вызывающего, а не жёсткий HERMES/None."""
    actor_id = new_id()
    async with uow_sessionmaker() as s:
        focus_set_id = await generate_daily_focus(
            s,
            date=datetime.date(2026, 7, 1),
            items=[],
            generated_by="user",
            actor_type=ActorType.USER,
            actor_id=actor_id,
        )
    async with uow_sessionmaker() as check:
        event = await _single_event(check, focus_set_id)
        assert event.actor_type == ActorType.USER
        assert event.actor_id == actor_id


@pytest.mark.asyncio
async def test_memory_store_requires_actor(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Без actor вызов падает сразу: молча приписать запись Hermes нельзя."""
    async with uow_sessionmaker() as s:
        with pytest.raises(TypeError):
            await memory_store(  # type: ignore[call-arg]
                s,
                memory_type=_unique_type("noactor"),
                content="x",
                confidence=0.5,
                source="hermes",
            )


@pytest.mark.asyncio
async def test_memory_event_records_caller_actor(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """memory.stored несёт автора вызывающего."""
    actor_id = new_id()
    async with uow_sessionmaker() as s:
        mem_id = await memory_store(
            s,
            memory_type=_unique_type("actor"),
            content="любит утро",
            confidence=0.7,
            source="hermes",
            actor_type=ActorType.HERMES,
            actor_id=actor_id,
        )
    async with uow_sessionmaker() as check:
        event = await _single_event(check, mem_id)
        assert event.actor_type == ActorType.HERMES
        assert event.actor_id == actor_id


@pytest.mark.asyncio
async def test_pattern_store_requires_actor(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Без actor вызов падает сразу: молча приписать запись Hermes нельзя."""
    async with uow_sessionmaker() as s:
        with pytest.raises(TypeError):
            await pattern_store(  # type: ignore[call-arg]
                s,
                pattern_type=_unique_type("noactor"),
                confidence=0.5,
                evidence_json={},
            )


@pytest.mark.asyncio
async def test_pattern_event_records_caller_actor(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """pattern.stored несёт автора вызывающего."""
    actor_id = new_id()
    async with uow_sessionmaker() as s:
        pid = await pattern_store(
            s,
            pattern_type=_unique_type("actor"),
            confidence=0.6,
            evidence_json={"n": 1},
            actor_type=ActorType.HERMES,
            actor_id=actor_id,
        )
    async with uow_sessionmaker() as check:
        event = await _single_event(check, pid)
        assert event.actor_type == ActorType.HERMES
        assert event.actor_id == actor_id
