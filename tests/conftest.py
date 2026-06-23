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

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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
    """Session-scoped async engine; teardown освобождает пул соединений."""
    engine = create_async_engine(database_url)
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


# TODO(план 03): добавить session-scoped фикстуру `migrated_db`, которая один раз
# прогоняет `alembic upgrade head` против `database_url` (миграция 001 создаёт
# таблицы/партиции/триггеры/REVOKE) ПЕРЕД интеграционными тестами планов 02–04.
# Сейчас миграций ещё нет — фикстура-якорь намеренно не реализована.
