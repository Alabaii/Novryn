"""Интеграция (критерий #1 / EVT-02): миграция 001 создаёт всю схему.

После `alembic upgrade head` против реального PostgreSQL 16 проверяем:
- наличие всех 8 таблиц в information_schema.tables;
- обязательные колонки events (8 полей + schema_version) и tasks;
- ключевые индексы idx_tasks_parent_task_id и idx_events_entity.

Все тесты зависят от session-scoped фикстуры `migrated_db` (conftest) и берут
свежий `AsyncSession` на каждый тест (Pitfall 3).
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_EXPECTED_TABLES = {
    "tasks",
    "task_dependencies",
    "attachments",
    "sessions",
    "daily_focus",
    "user_memory",
    "behavior_patterns",
    "events",
}

_EXPECTED_EVENT_COLUMNS = {
    "id",
    "occurred_at",
    "schema_version",
    "event_type",
    "entity_type",
    "entity_id",
    "actor_type",
    "actor_id",
    "payload_json",
}

_EXPECTED_TASK_COLUMNS = {
    "id",
    "title",
    "description",
    "status",
    "due_date",
    "parent_task_id",
    "user_time_estimate_minutes",
    "ai_time_estimate_minutes",
    "energy_required",
    "blocked_reason",
    "ai_context_json",
    "created_at",
    "updated_at",
    "completed_at",
    "archived_at",
}


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    """Свежая сессия поверх применённой миграции."""
    async with sessionmaker() as s:
        yield s


async def _column_names(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


@pytest.mark.asyncio
async def test_all_eight_tables_exist(msession: AsyncSession) -> None:
    rows = await msession.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    )
    present = {r[0] for r in rows}
    missing = _EXPECTED_TABLES - present
    assert not missing, f"Отсутствуют таблицы: {missing}"


@pytest.mark.asyncio
async def test_events_has_all_required_columns(msession: AsyncSession) -> None:
    cols = await _column_names(msession, "events")
    missing = _EXPECTED_EVENT_COLUMNS - cols
    assert not missing, f"events: нет колонок {missing}"


@pytest.mark.asyncio
async def test_tasks_has_prd_columns(msession: AsyncSession) -> None:
    cols = await _column_names(msession, "tasks")
    missing = _EXPECTED_TASK_COLUMNS - cols
    assert not missing, f"tasks: нет колонок {missing}"


@pytest.mark.asyncio
async def test_key_indexes_exist(msession: AsyncSession) -> None:
    rows = await msession.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    indexes = {r[0] for r in rows}
    assert "idx_tasks_parent_task_id" in indexes, indexes
    assert "idx_events_entity" in indexes, indexes
