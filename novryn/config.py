"""Конфигурация из окружения (.env): подключение к БД, глубина дерева, часовой пояс.

Секреты не хардкодятся (CLAUDE.md: secrets из env; threat T-01-06). `DATABASE_URL`
читается из переменных окружения; в dev-режиме `.env` подгружается через
`python-dotenv`. Драйвер обязан быть `asyncpg` (`postgresql+asyncpg://...`) —
синхронный `postgresql://` блокирует event loop (CLAUDE.md "What NOT to Use").
"""

import os
import zoneinfo

from dotenv import load_dotenv

# Подгружаем .env при импорте модуля (no-op, если файла нет — тогда берём из env).
load_dotenv()

_ASYNCPG_PREFIX = "postgresql+asyncpg://"

# Предел глубины дерева задач по умолчанию (D-12: реальные деревья мельче ~10).
_DEFAULT_TREE_MAX_DEPTH = 10

# Часовой пояс пользователя по умолчанию — как у контейнеров Docker.
_DEFAULT_TIMEZONE = "UTC"


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


def get_tree_max_depth() -> int:
    """Вернуть максимальную глубину дерева задач (D-12/HIER-03).

    Настраивается через ``NOVRYN_TREE_MAX_DEPTH``; по умолчанию ``10`` (D-12:
    реальные деревья мельче, guard ловит разбег рекурсии). ``get_task_tree``
    (план 04) передаёт это значение в фильтр глубины рекурсивного CTE и поднимает
    ``DepthExceededError`` при превышении — а не молча обрезает поддерево.

    Returns:
        Предел глубины как ``int`` (число уровней).

    Raises:
        ValueError: если ``NOVRYN_TREE_MAX_DEPTH`` задан, но не парсится в int.
    """
    raw = os.environ.get("NOVRYN_TREE_MAX_DEPTH")
    if raw is None:
        return _DEFAULT_TREE_MAX_DEPTH
    return int(raw)


def get_timezone() -> zoneinfo.ZoneInfo:
    """Вернуть часовой пояс пользователя (``NOVRYN_TIMEZONE``, IANA-имя).

    В нём считается «сегодня» — например, фокус дня без явной даты. По умолчанию
    ``UTC``: так живут контейнеры Docker, и без настройки после полуночи по местному
    времени «сегодня» было бы ещё «вчера».

    Returns:
        ``ZoneInfo`` пояса, например ``Europe/Moscow``.

    Raises:
        ValueError: если имя пояса неизвестно или некорректно — ошибка настройки
            должна остановить запуск, а не тихо сдвинуть даты.
    """
    name = os.environ.get("NOVRYN_TIMEZONE", _DEFAULT_TIMEZONE)
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"NOVRYN_TIMEZONE: неизвестный часовой пояс {name!r}; "
            "нужно IANA-имя, например Europe/Moscow"
        ) from exc
