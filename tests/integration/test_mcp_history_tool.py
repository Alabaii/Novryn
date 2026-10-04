"""Интеграция (Фаза 4, план 03): MCP-инструмент истории сущности.

Wave-0 RED baseline: ``novryn.mcp`` ещё не существует — фикстура ``mcp_server`` падает.

Поведение, фиксируемое тестами:
- novryn_get_entity_history отдаёт события сущности по порядку, с автором и полными
  снимками before/after;
- страницы с has_more / next_offset;
- тип сущности ограничен доменными сущностями: токены доступа через MCP не видны.
"""

from __future__ import annotations

import uuid

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.domain.events import ActorType, EventType
from novryn.services import task_service
from tests.integration.conftest import call_tool, structured


async def _task_with_history(
    maker: async_sessionmaker[AsyncSession], hermes_id: uuid.UUID
) -> uuid.UUID:
    """Задача с историей created (USER) → updated (USER) → completed (HERMES)."""
    async with maker() as s:
        task = await task_service.create_task(
            s, title="с историей", actor_type=ActorType.USER, actor_id=None
        )
    async with maker() as s:
        await task_service.update_task(
            s,
            task_id=task,
            actor_type=ActorType.USER,
            actor_id=None,
            changes={"title": "с историей (уточнено)"},
        )
    async with maker() as s:
        await task_service.complete_task(
            s, task_id=task, actor_type=ActorType.HERMES, actor_id=hermes_id
        )
    return task


@pytest.mark.asyncio
async def test_history_is_chronological_with_actors(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    hermes_id = new_id()
    task = await _task_with_history(sessionmaker, hermes_id)

    out = structured(
        await call_tool(
            mcp_server,
            "novryn_get_entity_history",
            {"entity_type": "task", "entity_id": str(task)},
        )
    )
    events = out["items"]
    assert [e["event_type"] for e in events] == [
        EventType.TASK_CREATED,
        EventType.TASK_UPDATED,
        EventType.TASK_COMPLETED,
    ]
    assert events[0]["payload_json"]["before"] is None
    assert events[1]["payload_json"]["after"]["title"] == "с историей (уточнено)"
    assert events[2]["actor_type"] == ActorType.HERMES
    assert events[2]["actor_id"] == str(hermes_id)
    assert out["has_more"] is False


@pytest.mark.asyncio
async def test_history_pages(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task = await _task_with_history(sessionmaker, new_id())
    page = structured(
        await call_tool(
            mcp_server,
            "novryn_get_entity_history",
            {"entity_type": "task", "entity_id": str(task), "limit": 2},
        )
    )
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    assert page["next_offset"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ["api_token", "nonsense"])
async def test_history_rejects_non_domain_entity_types(
    mcp_server: MCPServer, entity_type: str
) -> None:
    result = await call_tool(
        mcp_server,
        "novryn_get_entity_history",
        {"entity_type": entity_type, "entity_id": str(new_id())},
    )
    assert result.is_error
