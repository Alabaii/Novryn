"""Интеграция (Фаза 3, план 03 / MEM-03): memory_search — russian FTS + фильтры.

Wave-0 RED baseline: импортирует ещё НЕ существующий novryn.repositories.memory_read
(memory_search) — отсутствие модуля даёт collection-error (валидный RED до 03-03).

Поведение, фиксируемое тестом (структура дословно как test_search.py):
- русский FTS по content: множественное число матчит единственное (стемминг);
- комбинируемые структурные фильтры memory_type / min_confidence / source (D-09).

Сидинг прямыми insert (read-сторона, события не нужны); изоляция уникальными
memory_type. Свежая AsyncSession на операцию; зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import UserMemory

# Wave-2 целевой символ (ещё не существует → RED при коллекции).
from novryn.repositories.memory_read import memory_search


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _insert_mem(
    s: AsyncSession,
    *,
    memory_type: str,
    content: str,
    confidence: float,
    source: str,
) -> uuid.UUID:
    mid = new_id()
    await s.execute(
        insert(UserMemory).values(
            id=mid,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            source=source,
        )
    )
    return mid


@pytest.mark.asyncio
async def test_fts_and_filters(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MEM-03/D-09: русский FTS по content + комбинируемые фильтры."""
    tag = uuid.uuid4().hex[:8]
    t_doc = f"docs-{tag}"
    t_meet = f"meet-{tag}"
    async with uow_sessionmaker() as s:
        async with s.begin():
            doc = await _insert_mem(
                s, memory_type=t_doc, content="Важные документы пользователя",
                confidence=0.9, source="hermes",
            )
            await _insert_mem(
                s, memory_type=t_meet, content="Регулярные встречи по утрам",
                confidence=0.4, source="user",
            )

    async with uow_sessionmaker() as check:
        # FTS russian стемминг: 'документ' матчит 'документы'.
        by_fts = [m.id for m in await memory_search(check, q="документ")]
        assert doc in by_fts

        # Фильтр memory_type сужает.
        by_type = [m.id for m in await memory_search(check, memory_type=t_doc)]
        assert by_type == [doc]

        # min_confidence отсекает низкоуверенную память.
        by_conf = [
            m.memory_type
            for m in await memory_search(check, q="встречи", min_confidence=0.8)
        ]
        assert t_meet not in by_conf

        # FTS + структурный фильтр комбинируются.
        combined = [
            m.id
            for m in await memory_search(
                check, q="документы", memory_type=t_doc, source="hermes"
            )
        ]
        assert combined == [doc]
