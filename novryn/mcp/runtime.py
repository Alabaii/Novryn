"""Окружение инструментов: свежая сессия на вызов, «сегодня» пользователя, часы.

Одна ``AsyncSession`` на вызов инструмента (Anti-Pattern 5): сессия не делится между
конкурентными вызовами. Доменная ошибка внутри вызова становится ``ToolError`` —
понятным модели ответом с ``is_error=True``. Часы внедряются, а не берутся из
глобального ``now()``, чтобы «сегодня» проверялось тестами детерминированно.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.domain.errors import DomainError
from novryn.mcp.errors import to_tool_error

Clock = Callable[[], datetime.datetime]


def utc_now() -> datetime.datetime:
    """Текущий момент в UTC — часы сервера по умолчанию."""
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True)
class Runtime:
    """Зависимости инструментов: фабрика сессий, часовой пояс пользователя, часы."""

    sessionmaker: async_sessionmaker[AsyncSession]
    timezone: ZoneInfo
    clock: Clock = utc_now

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Свежая сессия на один вызов; доменная ошибка внутри блока → ``ToolError``."""
        try:
            async with self.sessionmaker() as session:
                yield session
        except DomainError as exc:
            raise to_tool_error(exc) from exc

    def today(self) -> datetime.date:
        """«Сегодня» в часовом поясе пользователя, а не сервера."""
        return self.clock().astimezone(self.timezone).date()
