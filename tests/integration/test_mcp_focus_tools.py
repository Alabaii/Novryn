"""Интеграция (Фаза 4, план 03): MCP-инструменты фокуса дня.

Wave-0 RED baseline: ``novryn.mcp`` ещё не существует — фикстура ``mcp_server`` падает.

Поведение, фиксируемое тестами:
- novryn_get_focus отдаёт последнюю версию снимка в порядке rank, и в каждом пункте —
  название и статус задачи (иначе модели пришлось бы делать вызов на каждую задачу);
- без даты берётся «сегодня» в часовом поясе пользователя, а не в UTC: часы сервера
  в тестах стоят на 22:30 UTC 3 июля, в Москве уже 4 июля;
- дата без снимка — ошибка с этой датой;
- novryn_focus_now отдаёт пункт с наивысшим приоритетом (rank 1).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.domain.events import ActorType
from novryn.services import focus_service, task_service
from tests.integration.conftest import MCP_TODAY, call_tool, error_text, structured


async def _task(maker: async_sessionmaker[AsyncSession], title: str) -> uuid.UUID:
    async with maker() as s:
        return await task_service.create_task(
            s, title=title, actor_type=ActorType.USER, actor_id=None
        )


async def _focus(
    maker: async_sessionmaker[AsyncSession],
    day: datetime.date,
    items: list[tuple[uuid.UUID, int, str | None]],
) -> uuid.UUID:
    async with maker() as s:
        return await focus_service.generate_daily_focus(
            s,
            date=day,
            items=[
                {"task_id": task_id, "rank": rank, "reason": reason}
                for task_id, rank, reason in items
            ],
            generated_by="hermes",
            actor_type=ActorType.HERMES,
            actor_id=None,
        )


@pytest.mark.asyncio
async def test_focus_is_ranked_with_task_details(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    day = datetime.date(2026, 7, 10)
    plan = await _task(sessionmaker, "Написать план")
    call = await _task(sessionmaker, "Созвон с бухгалтером")
    focus_set_id = await _focus(
        sessionmaker, day, [(call, 2, "после обеда"), (plan, 1, "главное")]
    )

    out = structured(
        await call_tool(mcp_server, "novryn_get_focus", {"date": day.isoformat()})
    )
    assert out["date"] == day.isoformat()
    assert out["focus_set_id"] == str(focus_set_id)
    assert out["generated_by"] == "hermes"
    assert [(i["rank"], i["title"], i["reason"]) for i in out["items"]] == [
        (1, "Написать план", "главное"),
        (2, "Созвон с бухгалтером", "после обеда"),
    ]
    assert out["items"][0]["task_id"] == str(plan)
    assert out["items"][0]["status"] == "INBOX"


@pytest.mark.asyncio
async def test_focus_defaults_to_today_in_user_timezone(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task = await _task(sessionmaker, "Сегодняшняя задача")
    await _focus(sessionmaker, MCP_TODAY, [(task, 1, None)])

    out = structured(await call_tool(mcp_server, "novryn_get_focus"))
    assert out["date"] == MCP_TODAY.isoformat()  # в UTC было бы ещё 3 июля
    assert [i["task_id"] for i in out["items"]] == [str(task)]


@pytest.mark.asyncio
async def test_missing_focus_is_reported(mcp_server: MCPServer) -> None:
    result = await call_tool(mcp_server, "novryn_get_focus", {"date": "1999-02-01"})
    assert result.is_error
    text = error_text(result)
    assert "1999-02-01" in text
    assert "не найден" in text


@pytest.mark.asyncio
async def test_focus_now_returns_top_task(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    day = datetime.date(2026, 7, 11)
    later = await _task(sessionmaker, "Потом")
    first = await _task(sessionmaker, "Сначала")
    await _focus(sessionmaker, day, [(later, 2, None), (first, 1, "самое важное")])

    out = structured(
        await call_tool(mcp_server, "novryn_focus_now", {"date": day.isoformat()})
    )
    assert out["date"] == day.isoformat()
    assert out["item"]["task_id"] == str(first)
    assert out["item"]["rank"] == 1
    assert out["item"]["title"] == "Сначала"
    assert out["item"]["reason"] == "самое важное"
