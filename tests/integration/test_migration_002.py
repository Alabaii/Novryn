"""Интеграция (Фаза 2, план 01): миграция 002 — soft-delete + russian FTS.

После ``alembic upgrade head`` (включает 002) против реального PostgreSQL 16:
- tasks.search_vector (generated) присутствует; attachments/task_dependencies.deleted_at присутствуют;
- idx_tasks_search_vector есть, старый simple idx_tasks_fts удалён (A9);
- uq_task_dependencies_active (partial unique) есть, полный uq_task_dependencies_pair удалён;
- повторная привязка soft-deleted пары (task_id, depends_on_task_id) проходит без
  UniqueViolation (D-06/D-07) — ключевое свойство soft-delete + re-link.

Зависит от session-scoped ``migrated_db`` (conftest); свежий ``AsyncSession`` на тест.
"""

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Task, TaskDependency


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    """Свежая сессия поверх применённой миграции (включая 002)."""
    async with sessionmaker() as s:
        yield s


async def _columns(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


async def _index_names(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_soft_delete_columns_present(msession: AsyncSession) -> None:
    assert "deleted_at" in await _columns(msession, "attachments")
    assert "deleted_at" in await _columns(msession, "task_dependencies")


@pytest.mark.asyncio
async def test_search_vector_column_present(msession: AsyncSession) -> None:
    assert "search_vector" in await _columns(msession, "tasks")


@pytest.mark.asyncio
async def test_fts_index_swapped(msession: AsyncSession) -> None:
    idx = await _index_names(msession)
    assert "idx_tasks_search_vector" in idx, idx
    assert "idx_tasks_fts" not in idx, idx


@pytest.mark.asyncio
async def test_partial_unique_replaces_full_constraint(msession: AsyncSession) -> None:
    idx = await _index_names(msession)
    assert "uq_task_dependencies_active" in idx, idx
    # Полный UNIQUE-constraint из 001 удалён (заменён частичным индексом).
    rows = await msession.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = 'uq_task_dependencies_pair'"
        )
    )
    assert rows.first() is None, "uq_task_dependencies_pair всё ещё существует"


@pytest.mark.asyncio
async def test_relink_after_soft_delete(msession: AsyncSession) -> None:
    """Soft-deleted пару (A→B) можно привязать заново — partial unique допускает."""
    a = Task(id=new_id(), title="A")
    b = Task(id=new_id(), title="B")
    msession.add_all([a, b])
    await msession.flush()

    # Первичная активная связь A→B.
    td1 = TaskDependency(id=new_id(), task_id=a.id, depends_on_task_id=b.id)
    msession.add(td1)
    await msession.flush()

    # Soft-delete: выставляем deleted_at (строка выпадает из активного частичного индекса).
    td1.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    await msession.flush()

    # Повторная привязка ТОЙ ЖЕ пары — INSERT проходит без UniqueViolation (D-06/D-07).
    td2 = TaskDependency(id=new_id(), task_id=a.id, depends_on_task_id=b.id)
    msession.add(td2)
    await msession.flush()
    await msession.commit()

    assert td2.id != td1.id
