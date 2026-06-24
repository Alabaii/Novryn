"""Интеграция (D-01/D-02/D-03): payload несёт полные снимки before/after.

Доказывает на реальном PostgreSQL 16:
- на UPDATE существующей сущности payload.before = ПРЕЖНЯЯ полная строка,
  payload.after = НОВАЯ полная строка (обе самодостаточны, D-02);
- тяжёлое ``ai_context_json`` присутствует ЦЕЛИКОМ в ОБОИХ снимках (D-03);
- before/after имеют одинаковую форму (единый сериализатор) — изменилось только
  обновлённое поле.

Свежая AsyncSession на операцию; зависит от ``migrated_db``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Task
from novryn.domain.events import ActorType, EventType
from novryn.repositories.uow import mutate_with_event


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _load_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


@pytest.mark.asyncio
async def test_update_snapshot_carries_full_before_and_after(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """before = прежняя строка, after = новая; ai_context_json целиком в обоих."""
    task_id = new_id()
    ai_ctx: dict[str, Any] = {
        "hermes_reasoning": "user works best in mornings",
        "confidence_factors": [1, 2, 3],
        "nested": {"deep": "value"},
    }

    async def create(session: AsyncSession) -> uuid.UUID:
        await session.execute(
            insert(Task).values(
                id=task_id,
                title="Исходный заголовок",
                status="TODO",
                ai_context_json=ai_ctx,
            )
        )
        return task_id

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_CREATED,
            actor_type=ActorType.USER,
            actor_id=None,
            apply=create,
            load_row=lambda s: _load_task(s, task_id),
        )

    async def rename(session: AsyncSession) -> None:
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(title="Обновлённый заголовок", status="IN_PROGRESS")
        )

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_UPDATED,
            actor_type=ActorType.USER,
            actor_id=None,
            apply=rename,
            load_row=lambda s: _load_task(s, task_id),
        )

    async with uow_sessionmaker() as check:
        update_event = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == task_id)
                .where(Event.event_type == EventType.TASK_UPDATED)
            )
        ).scalar_one()

        payload = update_event.payload_json
        before = cast(dict[str, Any], payload["before"])
        after = cast(dict[str, Any], payload["after"])

        # Оба снимка — полные строки (D-02): одинаковый набор ключей.
        assert before is not None and after is not None
        assert set(before.keys()) == set(after.keys())

        # before = прежнее состояние.
        assert before["title"] == "Исходный заголовок"
        assert before["status"] == "TODO"
        # after = новое состояние.
        assert after["title"] == "Обновлённый заголовок"
        assert after["status"] == "IN_PROGRESS"

        # ai_context_json присутствует ЦЕЛИКОМ в обоих снимках (D-03).
        assert before["ai_context_json"] == ai_ctx
        assert after["ai_context_json"] == ai_ctx


@pytest.mark.asyncio
async def test_before_snapshot_decoupled_from_after_identity(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CR-02 регресс: before-снимок не схлопывается в after через identity-map.

    На UPDATE ``load_row`` (session.get) внутри UoW возвращает ОДИН и тот же
    identity-map-инстанс для before и after. UoW сериализует before до apply и
    отвязывает инстанс (``session.expunge``), поэтому after грузится свежим, а
    before нельзя задним числом «обновить». Тест падает, если эта развязка
    снимется (before стал бы == after на изменённом поле) — прямая защита
    аудит-инварианта Novryn.
    """
    task_id = new_id()

    async def create(session: AsyncSession) -> None:
        await session.execute(
            insert(Task).values(id=task_id, title="v1", status="TODO")
        )

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_CREATED,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            apply=create,
            load_row=lambda s: _load_task(s, task_id),
        )

    async def advance(session: AsyncSession) -> None:
        await session.execute(
            update(Task).where(Task.id == task_id).values(status="IN_PROGRESS")
        )

    async with uow_sessionmaker() as session:
        await mutate_with_event(
            session,
            entity_type="task",
            entity_id=task_id,
            event_type=EventType.TASK_UPDATED,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            apply=advance,
            load_row=lambda s: _load_task(s, task_id),
        )

    async with uow_sessionmaker() as check:
        ev = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == task_id)
                .where(Event.event_type == EventType.TASK_UPDATED)
            )
        ).scalar_one()
        before = cast(dict[str, Any], ev.payload_json["before"])
        after = cast(dict[str, Any], ev.payload_json["after"])

        # Изменённое поле РАЗЛИЧАЕТСЯ: before хранит прежнее состояние, after — новое.
        assert before["status"] == "TODO"
        assert after["status"] == "IN_PROGRESS"
        assert before["status"] != after["status"]
        # Снимки самодостаточны и не являются одним и тем же объектом.
        assert before is not after
