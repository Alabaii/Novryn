"""Интеграция (Фаза 2, план 03 / TASK-01..06,11, HIER-01): create/get/update задачи.

- create+get round-trip: все PRD-поля (включая ai_context_json) сохраняются; ровно
  одно событие task.created (before=None, after=полная строка);
- update с реальным изменением → ровно одно task.updated (before/after различаются);
- update без изменений (test_update_only_on_diff) → возврат, НОВОГО события нет (D-08);
- get несуществующей → None; update несуществующей → NotFoundError.

Свежая AsyncSession на операцию (Pitfall 3); зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event
from novryn.domain.errors import NotFoundError
from novryn.domain.events import ActorType, EventType
from novryn.services import task_service


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий поверх мигрированной БД (сервис открывает UoW сам)."""
    return sessionmaker


async def _event_types(
    check: AsyncSession, task_id: uuid.UUID
) -> list[str]:
    rows = (
        await check.execute(
            select(Event)
            .where(Event.entity_id == task_id)
            .order_by(Event.occurred_at)
        )
    ).scalars().all()
    return [e.event_type for e in rows]


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """create_task со всеми полями → get_task отдаёт их целиком; 1 task.created."""
    actor_id = new_id()
    async with uow_sessionmaker() as s:
        task_id = await task_service.create_task(
            s,
            title="Большая задача",
            description="описание",
            actor_type=ActorType.USER,
            actor_id=actor_id,
            status="TODO",
            energy_required="HIGH",
            user_time_estimate_minutes=90,
            ai_context_json={"hermes_note": "приоритет", "score": 9},
        )

    async with uow_sessionmaker() as check:
        task = await task_service.get_task(check, task_id)
        assert task is not None
        assert task.title == "Большая задача"
        assert task.description == "описание"
        assert task.status == "TODO"
        assert task.energy_required == "HIGH"
        assert task.user_time_estimate_minutes == 90
        assert task.ai_context_json == {"hermes_note": "приоритет", "score": 9}

        events = (
            await check.execute(select(Event).where(Event.entity_id == task_id))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == EventType.TASK_CREATED
        assert events[0].payload_json["before"] is None
        assert events[0].payload_json["after"] is not None


@pytest.mark.asyncio
async def test_update_writes_event_on_change(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """update меняет поле → ровно одно task.updated; before/after различаются."""
    async with uow_sessionmaker() as s:
        task_id = await task_service.create_task(
            s, title="v1", actor_type=ActorType.USER, actor_id=None, status="TODO"
        )
    async with uow_sessionmaker() as s:
        await task_service.update_task(
            s,
            task_id=task_id,
            actor_type=ActorType.HERMES,
            actor_id=None,
            changes={"title": "v2"},
        )

    async with uow_sessionmaker() as check:
        assert await _event_types(check, task_id) == [
            EventType.TASK_CREATED,
            EventType.TASK_UPDATED,
        ]
        updated = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == task_id)
                .where(Event.event_type == EventType.TASK_UPDATED)
            )
        ).scalars().one()
        before = cast(dict[str, Any], updated.payload_json["before"])
        after = cast(dict[str, Any], updated.payload_json["after"])
        assert before["title"] == "v1"
        assert after["title"] == "v2"


@pytest.mark.asyncio
async def test_update_only_on_diff(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """update значениями, равными текущим → НОВОГО события нет (D-01/D-08)."""
    async with uow_sessionmaker() as s:
        task_id = await task_service.create_task(
            s, title="stable", actor_type=ActorType.USER, actor_id=None, status="TODO"
        )
    async with uow_sessionmaker() as s:
        result = await task_service.update_task(
            s,
            task_id=task_id,
            actor_type=ActorType.USER,
            actor_id=None,
            changes={"title": "stable", "status": "TODO"},  # без изменений
        )
        assert result == task_id

    async with uow_sessionmaker() as check:
        # Только исходный task.created — фантомного task.updated нет.
        assert await _event_types(check, task_id) == [EventType.TASK_CREATED]


@pytest.mark.asyncio
async def test_get_missing_returns_none(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with uow_sessionmaker() as check:
        assert await task_service.get_task(check, new_id()) is None


@pytest.mark.asyncio
async def test_update_missing_raises_not_found(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    missing = new_id()
    with pytest.raises(NotFoundError):
        async with uow_sessionmaker() as s:
            await task_service.update_task(
                s,
                task_id=missing,
                actor_type=ActorType.USER,
                actor_id=None,
                changes={"title": "x"},
            )
    # Транзакция не открывалась (UoW не вызван) — события нет.
    async with uow_sessionmaker() as check:
        count = await check.scalar(
            select(text("count(*)")).select_from(Event).where(Event.entity_id == missing)
        )
        assert count == 0
