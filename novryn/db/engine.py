"""Async-движок и фабрика сессий (SQLAlchemy 2.0 + asyncpg).

Движок строится лениво из `DATABASE_URL` (novryn/config.py) — импорт модуля не
требует наличия переменной окружения. `async_sessionmaker(expire_on_commit=False)`
обязателен в async-контексте: иначе ленивое истечение атрибутов после commit даёт
`MissingGreenlet` (ARCHITECTURE.md Pattern 3 / Anti-Pattern; CLAUDE.md).

Долгоживущая глобальная `AsyncSession` НЕ создаётся (Anti-Pattern 5) — потребители
(репозитории, conftest) открывают свежую сессию через `get_sessionmaker()`.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from novryn.config import get_database_url

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Вернуть процесс-широкий async-движок, создав его при первом обращении.

    Ленивое построение: `DATABASE_URL` читается только в момент первого вызова,
    поэтому импорт модуля безопасен даже без заданного окружения.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_database_url())
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Вернуть фабрику async-сессий (`expire_on_commit=False`).

    Переиспользуется conftest-фикстурами и репозиториями. Каждый вызов фабрики
    (`async with get_sessionmaker()() as s:`) даёт свежую короткоживущую сессию —
    глобальная долгоживущая сессия не используется (Anti-Pattern 5).
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker
