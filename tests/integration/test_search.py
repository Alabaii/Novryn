"""Интеграция (Фаза 2, план 04 / TASK-09): search_tasks — FTS + фильтры + сортировка.

- FTS russian: множественное число в title матчит единственное в q (стемминг);
- FTS english: ASCII-слова стеммятся english_stem внутри russian-конфига;
- ARCHIVED исключены по умолчанию (D-15), возвращаются по status/include_archived;
- фильтры (parent/energy/due) сужают и комбинируются (D-17);
- сортировка по created_at DESC; пагинация limit/offset (D-16).

Тесты изолированы уникальным parent_task_id (общая session-scoped БД); проверки —
через membership. Сидинг прямыми insert'ами (read-сторона, события не нужны).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Task
from novryn.repositories import task_read

_UTC = datetime.timezone.utc


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _insert(s: AsyncSession, **kw: object) -> uuid.UUID:
    tid = new_id()
    await s.execute(insert(Task).values(id=tid, **kw))
    return tid


@pytest.mark.asyncio
async def test_fts_russian_stemming(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """'документы' (plural) матчится запросом 'документ' (стемминг russian)."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            parent = await _insert(s, title="ru-parent", status="TODO")
            child = await _insert(
                s, title="Важные документы", status="TODO", parent_task_id=parent
            )
    async with uow_sessionmaker() as check:
        res = await task_read.search_tasks(check, q="документ", parent_task_id=parent)
        assert child in [t.id for t in res]


@pytest.mark.asyncio
async def test_fts_english_stemming(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """'meeting' матчится запросом 'meetings' (english_stem внутри russian-конфига)."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            parent = await _insert(s, title="en-parent", status="TODO")
            child = await _insert(
                s, title="weekly meeting notes", status="TODO", parent_task_id=parent
            )
    async with uow_sessionmaker() as check:
        res = await task_read.search_tasks(check, q="meetings", parent_task_id=parent)
        assert child in [t.id for t in res]


@pytest.mark.asyncio
async def test_archived_excluded_by_default_and_returned_explicitly(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with uow_sessionmaker() as s:
        async with s.begin():
            parent = await _insert(s, title="arch-parent", status="TODO")
            active = await _insert(
                s, title="active doc", status="TODO", parent_task_id=parent
            )
            archived = await _insert(
                s, title="archived doc", status="ARCHIVED", parent_task_id=parent
            )
    async with uow_sessionmaker() as check:
        default = [t.id for t in await task_read.search_tasks(check, parent_task_id=parent)]
        assert active in default
        assert archived not in default  # D-15

        only_arch = [
            t.id
            for t in await task_read.search_tasks(
                check, parent_task_id=parent, status="ARCHIVED"
            )
        ]
        assert only_arch == [archived]

        both = [
            t.id
            for t in await task_read.search_tasks(
                check, parent_task_id=parent, include_archived=True
            )
        ]
        assert active in both and archived in both


@pytest.mark.asyncio
async def test_filters_combine(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with uow_sessionmaker() as s:
        async with s.begin():
            parent = await _insert(s, title="filt-parent", status="TODO")
            high = await _insert(
                s,
                title="high energy",
                status="TODO",
                energy_required="HIGH",
                parent_task_id=parent,
                due_date=datetime.datetime(2026, 1, 10, tzinfo=_UTC),
            )
            low = await _insert(
                s,
                title="low energy",
                status="TODO",
                energy_required="LOW",
                parent_task_id=parent,
                due_date=datetime.datetime(2026, 2, 10, tzinfo=_UTC),
            )
    async with uow_sessionmaker() as check:
        by_energy = [
            t.id
            for t in await task_read.search_tasks(
                check, parent_task_id=parent, energy="HIGH"
            )
        ]
        assert by_energy == [high]

        by_due = [
            t.id
            for t in await task_read.search_tasks(
                check,
                parent_task_id=parent,
                due_to=datetime.datetime(2026, 1, 15, tzinfo=_UTC),
            )
        ]
        assert by_due == [high]
        assert low not in by_due


@pytest.mark.asyncio
async def test_sort_and_pagination(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with uow_sessionmaker() as s:
        async with s.begin():
            parent = await _insert(s, title="page-parent", status="TODO")
            c1 = await _insert(
                s,
                title="oldest",
                status="TODO",
                parent_task_id=parent,
                created_at=datetime.datetime(2026, 1, 1, tzinfo=_UTC),
            )
            c2 = await _insert(
                s,
                title="middle",
                status="TODO",
                parent_task_id=parent,
                created_at=datetime.datetime(2026, 1, 2, tzinfo=_UTC),
            )
            c3 = await _insert(
                s,
                title="newest",
                status="TODO",
                parent_task_id=parent,
                created_at=datetime.datetime(2026, 1, 3, tzinfo=_UTC),
            )
    async with uow_sessionmaker() as check:
        ordered = [
            t.id for t in await task_read.search_tasks(check, parent_task_id=parent)
        ]
        assert ordered == [c3, c2, c1]  # created_at DESC (D-16)

        page1 = [
            t.id
            for t in await task_read.search_tasks(
                check, parent_task_id=parent, limit=2
            )
        ]
        assert page1 == [c3, c2]

        page2 = [
            t.id
            for t in await task_read.search_tasks(
                check, parent_task_id=parent, limit=2, offset=2
            )
        ]
        assert page2 == [c1]
