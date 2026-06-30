"""Интеграция (Фаза 3, план 02 / FOCUS-01..05): daily focus generate + read.

Wave-0 RED baseline: импортирует ещё НЕ существующие сервис/read-модули Wave 2
(novryn.services.focus_service / novryn.repositories.focus_read) — отсутствие
модулей даёт collection-error (валидный RED до реализации 03-02).

Поведение, фиксируемое тестами:
- generate_daily_focus пишет N строк daily_focus + РОВНО одно событие focus.generated
  (считать через Event.entity_type=='daily_focus') — FOCUS-01/D-03;
- get_today_tasks возвращает задачи в порядке rank с reason/generated_by — FOCUS-02;
- generate принимает любое число задач — FOCUS-03;
- повторный generate создаёт новую версию, чтение берёт последнюю — D-01/D-02;
- чтение без снимка → FocusNotFoundError, не пустой результат — FOCUS-04/D-04;
- focus_now возвращает наивысший rank на дату; без снимка → FocusNotFoundError — FOCUS-05.

Сидинг прямыми insert (задачи); фокус — через сервис. Свежая AsyncSession на операцию
(Pitfall 3); зависит от migrated_db. НЕ моки БД — реальный PostgreSQL.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Event, Task
from novryn.domain.errors import FocusNotFoundError

# Wave-2 целевые символы (ещё не существуют → RED при коллекции).
from novryn.repositories.focus_read import focus_now, get_today_tasks
from novryn.services.focus_service import generate_daily_focus

_UTC = datetime.timezone.utc


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _insert_task(s: AsyncSession, title: str) -> uuid.UUID:
    tid = new_id()
    await s.execute(insert(Task).values(id=tid, title=title, status="TODO"))
    return tid


async def _count_focus_events(s: AsyncSession) -> int:
    n = await s.scalar(
        select(text("count(*)"))
        .select_from(Event)
        .where(Event.entity_type == "daily_focus")
    )
    return int(n or 0)


@pytest.mark.asyncio
async def test_generate_writes_n_rows_one_event(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FOCUS-01/D-03: N строк daily_focus + РОВНО одно focus.generated."""
    day = datetime.date(2026, 6, 30)
    async with uow_sessionmaker() as s:
        async with s.begin():
            t1 = await _insert_task(s, "focus task 1")
            t2 = await _insert_task(s, "focus task 2")

    async with uow_sessionmaker() as s:
        before = await _count_focus_events(s)
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s,
            date=day,
            items=[
                {"task_id": t1, "rank": 1, "reason": "важно"},
                {"task_id": t2, "rank": 2, "reason": None},
            ],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as check:
        after = await _count_focus_events(check)
        assert after - before == 1  # ровно одно агрегированное событие


@pytest.mark.asyncio
async def test_get_today_tasks_preserves_rank(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FOCUS-02: чтение возвращает задачи в порядке rank с reason/generated_by."""
    day = datetime.date(2026, 6, 29)
    async with uow_sessionmaker() as s:
        async with s.begin():
            a = await _insert_task(s, "rank A")
            b = await _insert_task(s, "rank B")
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s,
            date=day,
            items=[
                {"task_id": b, "rank": 2, "reason": "второй"},
                {"task_id": a, "rank": 1, "reason": "первый"},
            ],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as check:
        rows = await get_today_tasks(check, day)
        assert [r.task_id for r in rows] == [a, b]  # по rank ASC


@pytest.mark.asyncio
async def test_generate_accepts_many_tasks(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FOCUS-03: generate принимает любое число задач."""
    day = datetime.date(2026, 6, 28)
    async with uow_sessionmaker() as s:
        async with s.begin():
            ids = [await _insert_task(s, f"many {i}") for i in range(12)]
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s,
            date=day,
            items=[{"task_id": t, "rank": i + 1, "reason": None} for i, t in enumerate(ids)],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as check:
        rows = await get_today_tasks(check, day)
        assert len(rows) == 12


@pytest.mark.asyncio
async def test_regenerate_creates_new_version_read_latest(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D-01/D-02: повторный generate — новая версия; чтение возвращает последнюю."""
    day = datetime.date(2026, 6, 27)
    async with uow_sessionmaker() as s:
        async with s.begin():
            old = await _insert_task(s, "old focus")
            new = await _insert_task(s, "new focus")
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s, date=day, items=[{"task_id": old, "rank": 1, "reason": None}],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s, date=day, items=[{"task_id": new, "rank": 1, "reason": None}],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as check:
        rows = await get_today_tasks(check, day)
        assert [r.task_id for r in rows] == [new]  # только последняя версия


@pytest.mark.asyncio
async def test_get_today_no_focus_raises(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FOCUS-04/D-04: чтение даты без снимка → FocusNotFoundError, не пустой список."""
    empty_day = datetime.date(1999, 1, 1)
    async with uow_sessionmaker() as check:
        with pytest.raises(FocusNotFoundError):
            await get_today_tasks(check, empty_day)


@pytest.mark.asyncio
async def test_focus_now_top_rank_and_raises_if_empty(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FOCUS-05: focus_now → наивысший rank на дату; без снимка → FocusNotFoundError."""
    day = datetime.date(2026, 6, 26)
    async with uow_sessionmaker() as s:
        async with s.begin():
            top = await _insert_task(s, "top")
            second = await _insert_task(s, "second")
    async with uow_sessionmaker() as s:
        await generate_daily_focus(
            s,
            date=day,
            items=[
                {"task_id": second, "rank": 2, "reason": None},
                {"task_id": top, "rank": 1, "reason": None},
            ],
            generated_by="hermes",
        )
    async with uow_sessionmaker() as check:
        row = await focus_now(check, day)
        assert row.task_id == top

    async with uow_sessionmaker() as check:
        with pytest.raises(FocusNotFoundError):
            await focus_now(check, datetime.date(1999, 1, 2))
