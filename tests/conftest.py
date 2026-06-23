"""Общие pytest-фикстуры фазы 01: реальный PostgreSQL 16 через testcontainers.

Инфраструктура интеграционных тестов всей фазы. Фикстуры поднимают эфемерный
PostgreSQL 16 в Docker (минимум PG 13 для BEFORE-триггера на партиц. родителе —
RESEARCH A3; используем 16) и отдают свежий `AsyncSession` на каждый тест.

Если Docker недоступен (нет демона / WSL2 backend на Windows 11), интеграционные
фикстуры делают `pytest.skip`, а не падают на этапе коллекции — поэтому unit-тесты
(например `test_uuid7_monotonic.py`) собираются и проходят без Docker.

Драйвер подключения — `postgresql+asyncpg://` (asyncpg обязателен; `postgresql://`
блокирует event loop — CLAUDE.md "What NOT to Use").
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# asyncpg на Windows требует SelectorEventLoop, не Proactor (дефолт на win32).
# На ProactorEventLoop ошибка из statement (триггер/CHECK) запускает
# `Connection._cancel`, который не доходит до await → соединение виснет в
# "another operation is in progress", отравляя весь пул. Ставим политику
# процесс-широко ДО создания любого event loop (импорт conftest — до тестов).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _docker_available() -> bool:
    """Проверить, что Docker-демон отвечает (testcontainers пригоден к работе)."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[object]:
    """Session-scoped: поднять реальный PostgreSQL 16 через testcontainers.

    Skip (а не fail) при недоступном Docker — интеграционные тесты не должны
    ломать коллекцию unit-тестов на машине без Docker (RESEARCH §Environment).
    """
    if not _docker_available():
        pytest.skip("Docker required for integration tests (testcontainers)")

    # Импорт здесь, чтобы коллекция не падала, если Docker недоступен.
    from testcontainers.postgres import PostgresContainer

    # driver="asyncpg" → get_connection_url() сразу отдаёт postgresql+asyncpg://.
    container = PostgresContainer("postgres:16", driver="asyncpg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container: object) -> str:
    """Session-scoped: async-URL контейнера (postgresql+asyncpg://...).

    Также выставляет `DATABASE_URL` в окружение — план 03 (Alembic env.py)
    читает его при автоприменении миграций к контейнеру.
    """
    url: str = postgres_container.get_connection_url()  # type: ignore[attr-defined]
    assert url.startswith("postgresql+asyncpg://"), url
    os.environ["DATABASE_URL"] = url
    return url


@pytest_asyncio.fixture(scope="session")
async def async_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Session-scoped async engine; teardown освобождает пул соединений.

    NullPool: каждое соединение закрывается после использования и не
    переиспользуется между тестами. Это изолирует возможные «отравленные»
    соединения (например, после ожидаемой ошибки триггера/CHECK) — иначе
    asyncpg-соединение из пула может остаться в состоянии «operation in progress».
    """
    from sqlalchemy import NullPool

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def sessionmaker(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session-scoped фабрика сессий.

    `expire_on_commit=False` обязателен в async-контексте: иначе ленивое
    истечение атрибутов после commit приведёт к `MissingGreenlet`
    (ARCHITECTURE.md Anti-Pattern; CLAUDE.md "What NOT to Use").
    """
    return async_sessionmaker(async_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Function-scoped: НОВЫЙ AsyncSession на каждый тест.

    Один долгоживущий session между тестами не переиспользуется
    (STATE.md Pitfall 3; ARCHITECTURE.md Anti-Pattern 5).
    """
    async with sessionmaker() as s:
        yield s


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    """Session-scoped: один раз прогнать ``alembic upgrade head`` против контейнера.

    Миграция 001 (план 03) создаёт все таблицы, партиции events, append-only-триггер
    и REVOKE под novryn_app. Интеграционные тесты планов 02–04 зависят от этой
    фикстуры, поэтому видят полностью применённую схему.

    `database_url` уже выставил `DATABASE_URL` в окружение — env.py читает его при
    построении async-движка. Запуск синхронный (`command.upgrade`), но внутри env.py
    использует свой собственный event loop (`asyncio.run`) поверх asyncpg-движка.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    # script_location в alembic.ini задан относительно корня проекта — фиксируем CWD-
    # независимый абсолютный путь, чтобы запуск из любого каталога находил миграции.
    cfg.set_main_option(
        "script_location", str(project_root / "novryn" / "db" / "migrations")
    )
    command.upgrade(cfg, "head")
    return database_url
