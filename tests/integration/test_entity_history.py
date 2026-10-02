"""Интеграция (Фаза 4, план 01): хронология событий сущности из Event Store.

Wave-0 RED baseline: импортирует ещё НЕ существующий novryn.repositories.event_read
(get_entity_history) — отсутствие модуля даёт collection-error (валидный RED до 04-01).

Поведение, фиксируемое тестами:
- история возвращает события сущности в порядке их записи (created → updated → ...);
- события других сущностей и другого entity_type в историю не попадают;
- limit/offset листают историю без пропусков и повторов;
- для неизвестной сущности история пуста (не ошибка).

Свежая AsyncSession на операцию (Pitfall 3); зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.domain.events import ActorType, EventType
from novryn.repositories.event_read import get_entity_history
from novryn.repositories.event_repository import append_event
from novryn.services import task_service


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _task_with_three_events(
    maker: async_sessionmaker[AsyncSession], title: str
) -> uuid.UUID:
    """Задача с историей created → updated → completed."""
    async with maker() as s:
        task_id = await task_service.create_task(
            s, title=title, actor_type=ActorType.USER, actor_id=None, status="TODO"
        )
    async with maker() as s:
        await task_service.update_task(
            s,
            task_id=task_id,
            actor_type=ActorType.USER,
            actor_id=None,
            changes={"title": f"{title} (правка)"},
        )
    async with maker() as s:
        await task_service.complete_task(
            s, task_id=task_id, actor_type=ActorType.HERMES, actor_id=None
        )
    return task_id


@pytest.mark.asyncio
async def test_history_is_chronological(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """События сущности идут в порядке записи; первое — создание (before=None)."""
    task_id = await _task_with_three_events(uow_sessionmaker, "история")
    async with uow_sessionmaker() as check:
        history = await get_entity_history(check, entity_type="task", entity_id=task_id)
    assert [e.event_type for e in history] == [
        EventType.TASK_CREATED,
        EventType.TASK_UPDATED,
        EventType.TASK_COMPLETED,
    ]
    assert all(e.entity_id == task_id for e in history)
    assert history[0].payload_json["before"] is None
    assert history[-1].actor_type == ActorType.HERMES


@pytest.mark.asyncio
async def test_history_excludes_other_entities(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """События чужой задачи и чужого entity_type с тем же id в историю не попадают."""
    task_id = await _task_with_three_events(uow_sessionmaker, "своя")
    await _task_with_three_events(uow_sessionmaker, "чужая")
    # Событие другого типа сущности с тем же entity_id: фильтр по entity_type обязателен.
    async with uow_sessionmaker() as s:
        async with s.begin():
            await append_event(
                s,
                event_type="attachment.attached",
                entity_type="attachment",
                entity_id=task_id,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                payload_json={},
            )
    async with uow_sessionmaker() as check:
        history = await get_entity_history(check, entity_type="task", entity_id=task_id)
    assert len(history) == 3
    assert all(e.entity_type == "task" and e.entity_id == task_id for e in history)


@pytest.mark.asyncio
async def test_history_pagination(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """limit/offset листают историю без пропусков и повторов."""
    task_id = await _task_with_three_events(uow_sessionmaker, "страницы")
    async with uow_sessionmaker() as check:
        full = await get_entity_history(check, entity_type="task", entity_id=task_id)
        first = await get_entity_history(
            check, entity_type="task", entity_id=task_id, limit=2
        )
        rest = await get_entity_history(
            check, entity_type="task", entity_id=task_id, limit=2, offset=2
        )
    assert [e.id for e in first + rest] == [e.id for e in full]
    assert len(first) == 2
    assert len(rest) == 1


@pytest.mark.asyncio
async def test_history_unknown_entity_is_empty(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Для сущности без событий история пуста — это не ошибка."""
    async with uow_sessionmaker() as check:
        history = await get_entity_history(check, entity_type="task", entity_id=new_id())
    assert history == []
