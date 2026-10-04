"""Сборка MCP-сервера Novryn: инструменты поверх доменных сервисов (Фаза 4).

Зависимости передаются явно — фабрика сессий, часовой пояс пользователя, часы, —
поэтому сервер собирается одинаково для тестов (встроенный клиент SDK, тестовая БД)
и для эксплуатации (HTTP-транспорт, план 04-05).
"""

from __future__ import annotations

from importlib.metadata import version
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.mcp.runtime import Clock, Runtime, utc_now
from novryn.mcp.tools import focus, history, intelligence, tasks

_INSTRUCTIONS = """\
Novryn — источник истины о делах пользователя: задачи и их иерархия, зависимости, \
вложения, сессии работы, фокус дня, долгосрочная память и поведенческие паттерны. \
Novryn хранит факты и ничего не решает сам. Каждое изменение записано в журнал \
событий: путь сущности к текущему состоянию даёт novryn_get_entity_history.

Все id — UUID. Время в ответах — UTC (ISO 8601). «Сегодня» (фокус дня без даты) \
считается в часовом поясе пользователя: {timezone}."""


def create_server(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    timezone: ZoneInfo,
    clock: Clock = utc_now,
) -> MCPServer:
    """Собрать MCP-сервер Novryn со всеми инструментами.

    Args:
        sessionmaker: фабрика сессий — по свежей сессии на вызов инструмента.
        timezone: часовой пояс пользователя (``config.get_timezone``).
        clock: часы сервера; в тестах — фиксированный момент.
    """
    runtime = Runtime(sessionmaker=sessionmaker, timezone=timezone, clock=clock)
    server: MCPServer = MCPServer(
        name="novryn",
        title="Novryn",
        instructions=_INSTRUCTIONS.format(timezone=timezone.key),
        version=version("novryn"),
    )
    for register in (tasks.register, focus.register, intelligence.register, history.register):
        register(server, runtime)
    return server
