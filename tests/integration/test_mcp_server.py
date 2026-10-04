"""Интеграция (Фаза 4, план 03): каркас MCP-сервера Novryn.

Wave-0 RED baseline: ``novryn.mcp`` ещё не существует — фикстура ``mcp_server`` падает.

Поведение, фиксируемое тестами:
- сервер публикует ровно набор инструментов чтения с префиксом ``novryn_``;
- каждый помечен только-для-чтения, идемпотентным и замкнутым (не ходит вовне);
- у каждого есть описание и схема ответа;
- ни один инструмент не принимает автора (actor_type/actor_id): автор берётся из
  токена (04-04/04-05), подделать его через аргументы нельзя;
- инструкции сервера называют часовой пояс пользователя;
- непредвиденная ошибка (например, недоступная БД) не раскрывает клиенту деталей.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import Tool
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from novryn.db.ids import new_id
from tests.integration.conftest import MCP_TIMEZONE, call_tool, error_text

READ_TOOLS = {
    "novryn_get_task",
    "novryn_search_tasks",
    "novryn_get_task_tree",
    "novryn_get_dependencies",
    "novryn_get_resources",
    "novryn_get_sessions",
    "novryn_get_focus",
    "novryn_focus_now",
    "novryn_search_memory",
    "novryn_get_behavior_patterns",
    "novryn_task_insights",
    "novryn_user_insights",
    "novryn_get_entity_history",
}


async def _tools(server: MCPServer) -> list[Tool]:
    async with Client(server) as client:
        return (await client.list_tools()).tools


@pytest.mark.asyncio
async def test_publishes_read_tools(mcp_server: MCPServer) -> None:
    assert {t.name for t in await _tools(mcp_server)} == READ_TOOLS


@pytest.mark.asyncio
async def test_read_tools_are_annotated_read_only(mcp_server: MCPServer) -> None:
    for tool in await _tools(mcp_server):
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.idempotent_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name


@pytest.mark.asyncio
async def test_every_tool_is_described(mcp_server: MCPServer) -> None:
    for tool in await _tools(mcp_server):
        assert tool.title, tool.name
        assert tool.description and len(tool.description) > 20, tool.name
        assert tool.output_schema is not None, tool.name


@pytest.mark.asyncio
async def test_no_tool_accepts_actor(mcp_server: MCPServer) -> None:
    """Автор действия берётся из токена — во входных схемах его нет."""
    for tool in await _tools(mcp_server):
        params = set(tool.input_schema.get("properties", {}))
        assert not params & {"actor_type", "actor_id", "actor"}, tool.name


@pytest.mark.asyncio
async def test_instructions_name_user_timezone(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as client:
        assert client.instructions is not None
        assert MCP_TIMEZONE in client.instructions


@pytest_asyncio.fixture
async def broken_server() -> AsyncIterator[MCPServer]:
    """Сервер, чья БД недоступна: порт, на котором никто не слушает."""
    from novryn.mcp.server import create_server

    engine = create_async_engine(
        "postgresql+asyncpg://novryn:s3cret@127.0.0.1:1/novryn", poolclass=NullPool
    )
    try:
        maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine)
        yield create_server(maker, timezone=ZoneInfo(MCP_TIMEZONE))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_internal_error_reveals_nothing(broken_server: MCPServer) -> None:
    result = await call_tool(broken_server, "novryn_get_task", {"task_id": str(new_id())})
    assert result.is_error
    text = error_text(result)
    assert "novryn_get_task" in text
    for secret in ("s3cret", "127.0.0.1", "postgresql", "Connect"):
        assert secret not in text
