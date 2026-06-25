"""Интеграция (Фаза 2, план 05 / ATCH-01..04): attach/detach/get вложений.

- attach_resource → attachment.attached; get_resources отдаёт type/title/url/metadata;
- невалидный type → отклонён ck_attachments_type (ATCH-02);
- detach → soft-delete + attachment.detached, исчезает из get_resources; re-attach →
  НОВАЯ строка + событие, старая soft-deleted остаётся (D-07);
- detach отсутствующего/уже удалённого — no-op без события (D-08).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Attachment, Event, Task
from novryn.domain.events import ActorType, EventType
from novryn.repositories import dependency_read
from novryn.services import attachment_service


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
async def test_attach_and_get(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        att_id = await attachment_service.attach_resource(
            s,
            task_id=task_id,
            type="URL",
            title="Spec",
            url="http://example/spec",
            metadata_json={"pinned": True},
            actor_type=ActorType.USER,
            actor_id=None,
        )
    async with uow_sessionmaker() as check:
        resources = await dependency_read.get_resources(check, task_id)
        assert len(resources) == 1
        r = resources[0]
        assert r.id == att_id
        assert r.type == "URL"
        assert r.title == "Spec"
        assert r.url == "http://example/spec"
        assert r.metadata_json == {"pinned": True}
        events = (
            await check.execute(select(Event).where(Event.entity_id == att_id))
        ).scalars().all()
        assert [e.event_type for e in events] == [EventType.ATTACHMENT_ATTACHED]


@pytest.mark.asyncio
async def test_invalid_type_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    with pytest.raises(IntegrityError):
        async with uow_sessionmaker() as s:
            await attachment_service.attach_resource(
                s,
                task_id=task_id,
                type="BOGUS",  # нарушает ck_attachments_type
                actor_type=ActorType.USER,
                actor_id=None,
            )


@pytest.mark.asyncio
async def test_detach_and_reattach(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(uow_sessionmaker)
    async with uow_sessionmaker() as s:
        first_id = await attachment_service.attach_resource(
            s, task_id=task_id, type="DOCUMENT", actor_type=ActorType.USER, actor_id=None
        )
    async with uow_sessionmaker() as s:
        detached = await attachment_service.detach_resource(
            s, attachment_id=first_id, actor_type=ActorType.USER, actor_id=None
        )
    assert detached == first_id
    async with uow_sessionmaker() as check:
        assert await dependency_read.get_resources(check, task_id) == []
        old = await check.get(Attachment, first_id)
        assert old is not None and old.deleted_at is not None  # история сохранена
        detach_events = (
            await check.execute(
                select(Event)
                .where(Event.entity_id == first_id)
                .where(Event.event_type == EventType.ATTACHMENT_DETACHED)
            )
        ).scalars().all()
        assert len(detach_events) == 1

    async with uow_sessionmaker() as s:
        second_id = await attachment_service.attach_resource(
            s, task_id=task_id, type="DOCUMENT", actor_type=ActorType.USER, actor_id=None
        )
    assert second_id != first_id  # новая строка (D-07)
    async with uow_sessionmaker() as check:
        active = await dependency_read.get_resources(check, task_id)
        assert [a.id for a in active] == [second_id]


@pytest.mark.asyncio
async def test_detach_missing_is_noop(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def _count_detach(check: AsyncSession) -> int:
        n = await check.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == EventType.ATTACHMENT_DETACHED)
        )
        return int(n or 0)

    async with uow_sessionmaker() as check:
        before = await _count_detach(check)
    async with uow_sessionmaker() as s:
        result = await attachment_service.detach_resource(
            s, attachment_id=new_id(), actor_type=ActorType.USER, actor_id=None
        )
    assert result is None  # вложения нет — no-op
    async with uow_sessionmaker() as check:
        assert await _count_detach(check) == before  # события не записано (D-08)
