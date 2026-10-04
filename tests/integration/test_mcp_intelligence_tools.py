"""Интеграция (Фаза 4, план 03): MCP-инструменты памяти, паттернов и сводок.

Wave-0 RED baseline: ``novryn.mcp`` ещё не существует — фикстура ``mcp_server`` падает.

Поведение, фиксируемое тестами:
- novryn_search_memory: полнотекстовый поиск и фильтры, страницы с has_more;
  confidence — число (0.8), а не строка "0.80";
- novryn_get_behavior_patterns: фильтр по уверенности, evidence_json целиком;
- novryn_task_insights: сводка по задаче; неизвестная задача — ошибка;
- novryn_user_insights: сводка по пользователю в оговорённой форме.
"""

from __future__ import annotations

import uuid

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.domain.events import ActorType
from novryn.services import memory_service, pattern_service, session_service, task_service
from tests.integration.conftest import call_tool, error_text, structured

_HERMES = ActorType.HERMES


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _remember(
    maker: async_sessionmaker[AsyncSession],
    *,
    memory_type: str,
    content: str,
    confidence: float,
    source: str,
) -> uuid.UUID:
    async with maker() as s:
        return await memory_service.memory_store(
            s,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            source=source,
            actor_type=_HERMES,
            actor_id=None,
        )


@pytest.mark.asyncio
async def test_search_memory_filters_and_full_text(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    source = _unique("src")
    morning = await _remember(
        sessionmaker,
        memory_type=_unique("ритм"),
        content="Лучше всего работает утром",
        confidence=0.8,
        source=source,
    )
    await _remember(
        sessionmaker,
        memory_type=_unique("еда"),
        content="Не любит работать после обеда",
        confidence=0.4,
        source=source,
    )

    confident = structured(
        await call_tool(
            mcp_server, "novryn_search_memory", {"source": source, "min_confidence": 0.7}
        )
    )
    assert [m["id"] for m in confident["items"]] == [str(morning)]
    assert confident["items"][0]["confidence"] == 0.8
    assert confident["has_more"] is False

    by_text = structured(
        await call_tool(mcp_server, "novryn_search_memory", {"source": source, "q": "утром"})
    )
    assert [m["content"] for m in by_text["items"]] == ["Лучше всего работает утром"]


@pytest.mark.asyncio
async def test_search_memory_pages(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    source = _unique("pages")
    for i in range(3):
        await _remember(
            sessionmaker,
            memory_type=_unique(f"m{i}"),
            content=f"факт {i}",
            confidence=0.5,
            source=source,
        )
    page = structured(
        await call_tool(mcp_server, "novryn_search_memory", {"source": source, "limit": 2})
    )
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    assert page["next_offset"] == 2


@pytest.mark.asyncio
async def test_get_behavior_patterns(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    pattern_type = _unique("откладывает-звонки")
    async with sessionmaker() as s:
        await pattern_service.pattern_store(
            s,
            pattern_type=pattern_type,
            confidence=0.65,
            evidence_json={"sessions": 12, "пример": "перенёс звонок"},
            actor_type=_HERMES,
            actor_id=None,
        )

    out = structured(
        await call_tool(
            mcp_server, "novryn_get_behavior_patterns", {"pattern_type": pattern_type}
        )
    )
    (only,) = out["patterns"]
    assert only["pattern_type"] == pattern_type
    assert only["confidence"] == 0.65
    assert only["evidence_json"] == {"sessions": 12, "пример": "перенёс звонок"}

    stricter = structured(
        await call_tool(
            mcp_server,
            "novryn_get_behavior_patterns",
            {"pattern_type": pattern_type, "min_confidence": 0.9},
        )
    )
    assert stricter["patterns"] == []


@pytest.mark.asyncio
async def test_task_insights(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s:
        task = await task_service.create_task(
            s, title="со сводкой", actor_type=ActorType.USER, actor_id=None, status="TODO"
        )
    async with sessionmaker() as s:
        session_id = await session_service.start_session(
            s, task_id=task, planned_minutes=20, actor_type=ActorType.USER, actor_id=None
        )
    async with sessionmaker() as s:
        await session_service.finish_session(
            s,
            session_id=session_id,
            actual_minutes=35,
            result="PARTIAL",
            actor_type=ActorType.USER,
            actor_id=None,
        )

    out = structured(
        await call_tool(mcp_server, "novryn_task_insights", {"task_id": str(task)})
    )
    assert out["task_id"] == str(task)
    assert out["status"] == "TODO"
    assert out["sessions"]["count"] == 1
    assert out["sessions"]["actual_minutes_total"] == 35
    assert out["sessions"]["partial"] == 1
    assert out["focus"] == {"appearances": 0, "best_rank": None}


@pytest.mark.asyncio
async def test_task_insights_unknown_task(mcp_server: MCPServer) -> None:
    result = await call_tool(mcp_server, "novryn_task_insights", {"task_id": str(new_id())})
    assert result.is_error
    assert "не найдена" in error_text(result)


@pytest.mark.asyncio
async def test_user_insights_shape(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s:
        await task_service.create_task(
            s, title="для общей сводки", actor_type=ActorType.USER, actor_id=None
        )
    out = structured(await call_tool(mcp_server, "novryn_user_insights"))
    assert set(out) == {"tasks", "time", "top_patterns", "memory", "focus"}
    assert out["tasks"]["total"] >= 1
    assert out["tasks"]["by_status"]["INBOX"] >= 1
    assert isinstance(out["memory"]["by_type"], dict)
