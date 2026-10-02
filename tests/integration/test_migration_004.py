"""Интеграция (Фаза 4, план 02): миграция 004 — токены доступа + права роли приложения.

Wave-0 RED baseline: до миграции 004 таблицы api_tokens нет, а у ``novryn_app`` есть
только SELECT/INSERT на доменные таблицы — сервисы, делающие UPDATE, под этой ролью
не работают, и защита журнала правами в реальной эксплуатации не действует.

После ``alembic upgrade head`` (включает 004) против реального PostgreSQL 16:
- api_tokens с CHECK держателя (USER/HERMES) и UNIQUE по хэшу токена;
- novryn_app может UPDATE доменные таблицы, которые сервисы обновляют на месте;
  daily_focus (версии снимков) и events (журнал) остаются без UPDATE;
- в api_tokens приложение может обновить ТОЛЬКО revoked_at (отзыв), но не хэш/держателя;
- ни на одну таблицу у приложения нет DELETE/TRUNCATE (только soft-delete);
- каждая таблица схемы доступна novryn_app на SELECT/INSERT (страж от забытого GRANT
  в будущих миграциях);
- downgrade 004 → upgrade 004 чисто.

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

# Таблицы, которые сервисы обновляют на месте (UPDATE / soft-delete через deleted_at).
_UPDATABLE_TABLES = (
    "tasks",
    "task_dependencies",
    "attachments",
    "sessions",
    "user_memory",
    "behavior_patterns",
)
# Только дописываются: версии снимков фокуса и журнал событий.
_APPEND_ONLY_TABLES = ("daily_focus", "events")


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    """Свежая сессия поверх применённой миграции (включая 004)."""
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


async def _constraint_exists(session: AsyncSession, name: str) -> bool:
    rows = await session.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = :n"
        ),
        {"n": name},
    )
    return rows.first() is not None


async def _table_privilege(session: AsyncSession, table: str, privilege: str) -> bool:
    return bool(
        await session.scalar(
            text("SELECT has_table_privilege('novryn_app', :t, :p)"),
            {"t": table, "p": privilege},
        )
    )


async def _column_privilege(
    session: AsyncSession, table: str, column: str, privilege: str
) -> bool:
    return bool(
        await session.scalar(
            text("SELECT has_column_privilege('novryn_app', :t, :c, :p)"),
            {"t": table, "c": column, "p": privilege},
        )
    )


async def _schema_tables(session: AsyncSession) -> list[str]:
    """Все таблицы схемы public, кроме партиций events и служебной alembic_version."""
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND NOT c.relispartition AND c.relname <> 'alembic_version' "
            "ORDER BY c.relname"
        )
    )
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_api_tokens_table_present(msession: AsyncSession) -> None:
    assert await _columns(msession, "api_tokens") == {
        "id",
        "name",
        "holder_type",
        "token_hash",
        "created_at",
        "revoked_at",
    }
    assert await _constraint_exists(msession, "ck_api_tokens_holder_type")
    assert await _constraint_exists(msession, "uq_api_tokens_token_hash")


@pytest.mark.asyncio
async def test_app_role_can_update_domain_tables(msession: AsyncSession) -> None:
    for table in _UPDATABLE_TABLES:
        assert await _table_privilege(msession, table, "UPDATE"), table


@pytest.mark.asyncio
async def test_append_only_tables_stay_without_update(msession: AsyncSession) -> None:
    for table in _APPEND_ONLY_TABLES:
        assert not await _table_privilege(msession, table, "UPDATE"), table


@pytest.mark.asyncio
async def test_api_tokens_only_revocation_is_updatable(msession: AsyncSession) -> None:
    assert await _column_privilege(msession, "api_tokens", "revoked_at", "UPDATE")
    for column in ("id", "name", "holder_type", "token_hash", "created_at"):
        assert not await _column_privilege(msession, "api_tokens", column, "UPDATE"), column


@pytest.mark.asyncio
async def test_every_table_readable_and_insertable_never_deletable(
    msession: AsyncSession,
) -> None:
    tables = await _schema_tables(msession)
    assert "api_tokens" in tables
    for table in tables:
        assert await _table_privilege(msession, table, "SELECT"), table
        assert await _table_privilege(msession, table, "INSERT"), table
        assert not await _table_privilege(msession, table, "DELETE"), table
        assert not await _table_privilege(msession, table, "TRUNCATE"), table


def _alembic_cfg() -> "Config":
    from alembic.config import Config

    # Этот файл — tests/integration/...; корень проекта на два уровня выше.
    project_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(project_root / "novryn" / "db" / "migrations")
    )
    return cfg


async def _schema_snapshot(database_url: str) -> dict[str, bool]:
    """Снимок наличия артефактов 004 через свежий короткоживущий async-движок."""
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            return {
                "tokens": bool(await _columns(s, "api_tokens")),
                "update_tasks": await _table_privilege(s, "tasks", "UPDATE"),
            }
    finally:
        await engine.dispose()


def test_downgrade_then_upgrade_004_is_clean(migrated_db: str) -> None:
    """004 обратима: downgrade снимает таблицу и права, upgrade head возвращает их.

    Синхронный тест: ``command.downgrade``/``upgrade`` внутри env.py поднимают
    собственный ``asyncio.run`` — нельзя вызывать из работающего event loop.
    """
    import asyncio

    from alembic import command

    assert os.environ.get("DATABASE_URL", "").startswith("postgresql+asyncpg://")
    cfg = _alembic_cfg()

    command.downgrade(cfg, "003")
    assert asyncio.run(_schema_snapshot(migrated_db)) == {
        "tokens": False,
        "update_tasks": False,
    }

    command.upgrade(cfg, "head")
    assert asyncio.run(_schema_snapshot(migrated_db)) == {
        "tokens": True,
        "update_tasks": True,
    }
