"""Конфигурация подключения к БД из окружения (.env).

Секреты не хардкодятся (CLAUDE.md: secrets из env; threat T-01-06). `DATABASE_URL`
читается из переменных окружения; в dev-режиме `.env` подгружается через
`python-dotenv`. Драйвер обязан быть `asyncpg` (`postgresql+asyncpg://...`) —
синхронный `postgresql://` блокирует event loop (CLAUDE.md "What NOT to Use").
"""

import os

from dotenv import load_dotenv

# Подгружаем .env при импорте модуля (no-op, если файла нет — тогда берём из env).
load_dotenv()

_ASYNCPG_PREFIX = "postgresql+asyncpg://"


def get_database_url() -> str:
    """Вернуть `DATABASE_URL` из окружения.

    Returns:
        Async-URL вида ``postgresql+asyncpg://user:pass@host:port/db``.

    Raises:
        RuntimeError: если ``DATABASE_URL`` не задан в окружении (.env) — это
            обязательный секрет подключения, дефолта быть не должно.
        ValueError: если URL использует синхронный драйвер вместо asyncpg —
            синхронный драйвер блокирует event loop в async-контексте.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL не задан. Укажите его в .env или окружении, например: "
            f"{_ASYNCPG_PREFIX}user:pass@localhost:5432/novryn"
        )
    if not url.startswith(_ASYNCPG_PREFIX):
        raise ValueError(
            "DATABASE_URL должен использовать asyncpg-драйвер "
            f"({_ASYNCPG_PREFIX}...); синхронный драйвер блокирует event loop. "
            f"Получено: {url.split('://', 1)[0]}://..."
        )
    return url
