"""Интеграция (Фаза 3, план 05 / INS-01/02): cross-domain insights aggregates.

Wave-0 RED baseline: импортирует ещё НЕ существующий novryn.repositories.insights_read
(task_insights / user_insights) — отсутствие модуля даёт collection-error (валидный
RED до 03-05).

Поведение, фиксируемое тестами:
- task_insights(task_id) даёт кросс-доменный срез по одной задаче: статус, статистика
  сессий, появления в фокусе, счётчики подзадач/зависимостей/вложений — INS-01;
- user_insights() даёт полный срез пользователя: задачи по статусам, отслеженное время,
  доля завершённых, топ паттернов, сводка памяти, активность фокуса — INS-02.

Сидинг прямыми insert (задача + сессии + фокус + память); read-only, события не нужны.
Assert «возвращает агрегаты без ошибок». Свежая AsyncSession; зависит от migrated_db.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import DailyFocus, Session, Task, UserMemory

# Wave-2 целевые символы (ещё не существуют → RED при коллекции).
from novryn.repositories.insights_read import task_insights, user_insights

_UTC = datetime.timezone.utc


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _seed(s: AsyncSession) -> uuid.UUID:
    """Засеять одну задачу + сессию + снимок фокуса + память."""
    tid = new_id()
    await s.execute(insert(Task).values(id=tid, title="insights task", status="DONE"))
    await s.execute(
        insert(Session).values(
            id=new_id(),
            task_id=tid,
            planned_minutes=30,
            actual_minutes=25,
            result="COMPLETED",
        )
    )
    await s.execute(
        insert(DailyFocus).values(
            id=new_id(),
            date=datetime.date(2026, 6, 30),
            task_id=tid,
            rank=1,
            reason="insights",
        )
    )
    await s.execute(
        insert(UserMemory).values(
            id=new_id(),
            memory_type=f"ins-{uuid.uuid4().hex[:10]}",
            content="любит фокус",
            confidence=0.7,
            source="hermes",
        )
    )
    return tid


@pytest.mark.asyncio
async def test_task_insights_aggregates(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """INS-01: task_insights возвращает кросс-доменный срез по задаче без ошибок."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            tid = await _seed(s)
    async with uow_sessionmaker() as check:
        result = await task_insights(check, tid)
        assert result is not None


@pytest.mark.asyncio
async def test_user_insights_aggregates(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """INS-02: user_insights возвращает полный кросс-доменный срез без ошибок."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            await _seed(s)
    async with uow_sessionmaker() as check:
        result = await user_insights(check)
        assert result is not None
