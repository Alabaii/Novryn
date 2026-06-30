"""Интеграция (Фаза 3, план 01): миграция 003 — focus versioning + memory FTS.

После ``alembic upgrade head`` (включает 003) против реального PostgreSQL 16:
- daily_focus.generated_at присутствует; idx_daily_focus_date_gen создан;
- user_memory.search_vector (generated) присутствует; idx_user_memory_search_vector есть;
- constraint'ы uq_user_memory_type / uq_behavior_pattern_type присутствуют;
- memory_type / pattern_type → NOT NULL;
- CHECK confidence (ck_user_memory_confidence/ck_behavior_confidence) НЕ удалён;
- downgrade 003 → upgrade 003 чисто (цепочка обратима).

Зависит от session-scoped ``migrated_db`` (conftest); свежий ``AsyncSession`` на тест.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from alembic.config import Config


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    """Свежая сессия поверх применённой миграции (включая 003)."""
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


async def _constraint_exists(session: AsyncSession, name: str) -> bool:
    rows = await session.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = :n"
        ),
        {"n": name},
    )
    return rows.first() is not None


async def _is_not_nullable(session: AsyncSession, table: str, column: str) -> bool:
    rows = await session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    row = rows.first()
    return row is not None and row[0] == "NO"


@pytest.mark.asyncio
async def test_generated_at_column_present(msession: AsyncSession) -> None:
    assert "generated_at" in await _columns(msession, "daily_focus")


@pytest.mark.asyncio
async def test_memory_search_vector_present(msession: AsyncSession) -> None:
    assert "search_vector" in await _columns(msession, "user_memory")


@pytest.mark.asyncio
async def test_new_indexes_present(msession: AsyncSession) -> None:
    idx = await _index_names(msession)
    assert "idx_daily_focus_date_gen" in idx, idx
    assert "idx_user_memory_search_vector" in idx, idx


@pytest.mark.asyncio
async def test_upsert_unique_constraints_present(msession: AsyncSession) -> None:
    assert await _constraint_exists(msession, "uq_user_memory_type")
    assert await _constraint_exists(msession, "uq_behavior_pattern_type")


@pytest.mark.asyncio
async def test_type_columns_not_null(msession: AsyncSession) -> None:
    assert await _is_not_nullable(msession, "user_memory", "memory_type")
    assert await _is_not_nullable(msession, "behavior_patterns", "pattern_type")


@pytest.mark.asyncio
async def test_confidence_checks_preserved(msession: AsyncSession) -> None:
    # D-08: CHECK confidence НЕ удалён/переобъявлён миграцией 003.
    assert await _constraint_exists(msession, "ck_user_memory_confidence")
    assert await _constraint_exists(msession, "ck_behavior_confidence")


def _alembic_cfg() -> "Config":
    from alembic.config import Config

    # Этот файл — tests/integration/...; корень проекта на два уровня выше.
    project_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(project_root / "novryn" / "db" / "migrations")
    )
    return cfg


async def _schema_snapshot(database_url: str) -> dict[str, object]:
    """Снимок наличия артефактов 003 через свежий короткоживущий async-движок."""
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            return {
                "focus_gen": "generated_at" in await _columns(s, "daily_focus"),
                "mem_sv": "search_vector" in await _columns(s, "user_memory"),
                "idx": await _index_names(s),
                "uq_mem": await _constraint_exists(s, "uq_user_memory_type"),
                "uq_pat": await _constraint_exists(s, "uq_behavior_pattern_type"),
            }
    finally:
        await engine.dispose()


def test_downgrade_then_upgrade_003_is_clean(migrated_db: str) -> None:
    """003 обратима: downgrade -1 снимает артефакты, upgrade head возвращает их.

    Синхронный тест: ``command.downgrade``/``upgrade`` внутри env.py поднимают
    собственный ``asyncio.run`` — нельзя вызывать из работающего event loop, поэтому
    проверки схемы делаем через изолированный ``asyncio.run(_schema_snapshot(...))``.
    """
    import asyncio

    from alembic import command

    assert os.environ.get("DATABASE_URL", "").startswith("postgresql+asyncpg://")
    cfg = _alembic_cfg()

    # Откат 003 → артефактов нет.
    command.downgrade(cfg, "002")
    snap = asyncio.run(_schema_snapshot(migrated_db))
    assert snap["focus_gen"] is False
    assert snap["mem_sv"] is False
    idx_down = snap["idx"]
    assert isinstance(idx_down, set)
    assert "idx_daily_focus_date_gen" not in idx_down
    assert "idx_user_memory_search_vector" not in idx_down
    assert snap["uq_mem"] is False
    assert snap["uq_pat"] is False

    # Повторный upgrade head → артефакты вернулись (цепочка цела для остальных тестов).
    command.upgrade(cfg, "head")
    snap = asyncio.run(_schema_snapshot(migrated_db))
    assert snap["focus_gen"] is True
    assert snap["mem_sv"] is True
    idx_up = snap["idx"]
    assert isinstance(idx_up, set)
    assert "idx_daily_focus_date_gen" in idx_up
    assert "idx_user_memory_search_vector" in idx_up
    assert snap["uq_mem"] is True
    assert snap["uq_pat"] is True
