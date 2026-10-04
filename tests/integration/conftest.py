"""Интеграционные хелперы Фазы 2 (поверх session-scoped фикстур tests/conftest.py).

``count_queries`` — счётчик SQL-исполнений для доказательства отсутствия N+1
(HIER-02, тест дерева в плане 04). КРИТИЧНО (Pitfall 3): listener вешается на
``async_engine.sync_engine``, НЕ на ``async_engine`` — у async-движка нет события
``before_cursor_execute`` напрямую, и счётчик молча остался бы в нуле (тест «одна
запрос» прошёл бы ложно). Здесь же он живёт, чтобы соседние интеграционные тесты
импортировали его как ``from tests.integration.conftest import count_queries``.

MCP (Фаза 4): фикстура ``mcp_server`` собирает сервер поверх тестовой БД с
фиксированными часами, ``call_tool`` вызывает инструмент через встроенный клиент SDK
(без сети), ``structured``/``error_text`` достают ответ или текст ошибки.
"""

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# «Сейчас» для MCP-сервера в тестах: 22:30 UTC 3 июля — в Москве (UTC+3) уже 4 июля.
# Так тесты «сегодня» отличают часовой пояс пользователя от UTC.
MCP_TIMEZONE = "Europe/Moscow"
MCP_NOW = datetime.datetime(2026, 7, 3, 22, 30, tzinfo=datetime.UTC)
MCP_TODAY = datetime.date(2026, 7, 4)


@contextmanager
def count_queries(async_engine: AsyncEngine) -> Iterator[dict[str, int]]:
    """Считать число SQL cursor-исполнений внутри блока ``with`` (no-N+1 guard).

    Вешает ``before_cursor_execute`` на синхронный движок под async-обёрткой
    (``async_engine.sync_engine`` — Pitfall 3) и инкрементит счётчик на каждом
    реальном исполнении курсора. Снимает listener в ``finally``.

    Использование::

        with count_queries(async_engine) as counter:
            await service.get_task_tree(session, root_id)
        assert counter["n"] == 1  # ровно один запрос — нет N+1

    Args:
        async_engine: session-scoped async-движок (фикстура tests/conftest.py).

    Yields:
        dict ``{"n": <число исполнений>}``; читать ПОСЛЕ выхода из блока with.
    """
    counter: dict[str, int] = {"n": 0}
    sync_engine = async_engine.sync_engine

    def _on_exec(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        counter["n"] += 1

    event.listen(sync_engine, "before_cursor_execute", _on_exec)
    try:
        yield counter
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_exec)


@pytest.fixture
def mcp_server(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> MCPServer:
    """MCP-сервер Novryn поверх тестовой БД с фиксированными часами ``MCP_NOW``."""
    # Импорт внутри фикстуры: пока novryn.mcp нет (RED), падают только MCP-тесты,
    # а не коллекция всех интеграционных тестов.
    from novryn.mcp.server import create_server

    return create_server(
        sessionmaker, timezone=ZoneInfo(MCP_TIMEZONE), clock=lambda: MCP_NOW
    )


async def call_tool(
    server: MCPServer, name: str, arguments: dict[str, Any] | None = None
) -> CallToolResult:
    """Вызвать инструмент через встроенный клиент SDK (в памяти, без сети).

    Клиент открывается на каждый вызов, а не в async-фикстуре: cancel scope anyio
    должен закрываться в той же задаче, где открыт.
    """
    async with Client(server) as client:
        return await client.call_tool(name, arguments or {})


def structured(result: CallToolResult) -> dict[str, Any]:
    """Структурированный ответ успешного вызова."""
    assert not result.is_error, error_text(result)
    content = result.structured_content
    assert isinstance(content, dict)
    return content


def error_text(result: CallToolResult) -> str:
    """Текст ответа (для ошибки — сообщение, которое увидит модель)."""
    return "".join(b.text for b in result.content if isinstance(b, TextContent))
