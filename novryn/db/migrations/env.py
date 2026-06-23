"""Async Alembic environment для Novryn (план 01-03, Task 1).

Рабочий путь — online async: строим ``AsyncEngine`` из ``DATABASE_URL`` (окружение,
через :func:`novryn.config.get_database_url`), открываем соединение и прогоняем
миграции синхронно внутри ``connection.run_sync`` (Alembic — синхронный движок;
async-движок отдаёт sync-фасад через ``run_sync``). URL/пароль НЕ хардкодятся в
``alembic.ini`` (CLAUDE.md: секреты из env; threat T-01-06).

``target_metadata = Base.metadata`` (источник колонок для compare/autogenerate);
``import novryn.db.models`` обязателен, чтобы все таблицы зарегистрировались в
metadata. ``compare_check_constraints=True`` — opt-in сравнение CHECK (RESEARCH
Pattern 6). Миграция 001 пишется ВРУЧНУЮ: autogenerate не видит партиции, триггеры,
REVOKE и CHECK — поэтому полагаться на него для 001 нельзя.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Регистрируем все ORM-модели в Base.metadata (import ради side-effect).
import novryn.db.models  # noqa: F401
from novryn.config import get_database_url
from novryn.db.base import Base

# Alembic Config: доступ к значениям alembic.ini.
config = context.config

# Логирование из alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Источник metadata для compare/autogenerate — единый Base всех моделей.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline-режим: configure только по URL, без живого соединения.

    Поддержан ради полноты (стандартный шаблон). Рабочий путь — online async.
    URL берётся из окружения, не из alembic.ini.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_check_constraints=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Сконфигурировать context на живом (sync-фасадном) соединении и прогнать."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_check_constraints=True,  # opt-in CHECK сравнение (RESEARCH Pattern 6)
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Построить AsyncEngine из DATABASE_URL и прогнать миграции через run_sync."""
    connectable = create_async_engine(get_database_url())

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online-режим: запустить async-runner в собственном event loop."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
