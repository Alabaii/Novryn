"""Интеграция (Фаза 4, план 03): MCP-инструменты чтения задач.

Wave-0 RED baseline: ``novryn.mcp`` ещё не существует — фикстура ``mcp_server`` падает.

Поведение, фиксируемое тестами:
- novryn_get_task отдаёт полную карточку задачи (без служебного search_vector);
  неизвестная задача — ошибка с id и подсказкой, где взять верный id;
- novryn_search_tasks листает страницы: has_more / next_offset; русский полнотекстовый
  поиск; время без часового пояса и слишком большая страница отвергаются на входе;
- novryn_get_task_tree отдаёт вложенное дерево;
- зависимости, вложения (только активные) и сессии задачи; для неизвестной задачи —
  ошибка, а не пустой список, который модель приняла бы за «ничего нет».
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.domain.events import ActorType
from novryn.services import (
    attachment_service,
    dependency_service,
    session_service,
    task_service,
)
from tests.integration.conftest import call_tool, error_text, structured

_USER = ActorType.USER


async def _create(
    maker: async_sessionmaker[AsyncSession], title: str, **fields: Any
) -> uuid.UUID:
    async with maker() as s:
        return await task_service.create_task(
            s, title=title, actor_type=_USER, actor_id=None, **fields
        )


async def _subtasks(
    maker: async_sessionmaker[AsyncSession], parent: uuid.UUID, *titles: str
) -> list[uuid.UUID]:
    async with maker() as s:
        return await task_service.create_subtasks(
            s,
            parent_task_id=parent,
            subtasks=[{"title": t} for t in titles],
            actor_type=_USER,
            actor_id=None,
        )


@pytest.mark.asyncio
async def test_get_task_returns_full_card(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    due = datetime.datetime(2026, 7, 20, 9, 0, tzinfo=datetime.UTC)
    task_id = await _create(
        sessionmaker,
        "Подготовить отчёт",
        description="квартальный",
        status="TODO",
        due_date=due,
        energy_required="HIGH",
        user_time_estimate_minutes=90,
        ai_context_json={"почему": "дедлайн"},
    )
    card = structured(
        await call_tool(mcp_server, "novryn_get_task", {"task_id": str(task_id)})
    )
    assert card["id"] == str(task_id)
    assert card["title"] == "Подготовить отчёт"
    assert card["description"] == "квартальный"
    assert card["status"] == "TODO"
    assert card["energy_required"] == "HIGH"
    assert card["user_time_estimate_minutes"] == 90
    assert card["ai_context_json"] == {"почему": "дедлайн"}
    assert datetime.datetime.fromisoformat(card["due_date"]) == due
    assert "search_vector" not in card


@pytest.mark.asyncio
async def test_unknown_task_is_actionable_error(mcp_server: MCPServer) -> None:
    missing = new_id()
    result = await call_tool(mcp_server, "novryn_get_task", {"task_id": str(missing)})
    assert result.is_error
    text = error_text(result)
    assert str(missing) in text
    assert "не найдена" in text
    assert "novryn_search_tasks" in text  # где взять верный id


@pytest.mark.asyncio
async def test_invalid_task_id_rejected(mcp_server: MCPServer) -> None:
    result = await call_tool(mcp_server, "novryn_get_task", {"task_id": "не-uuid"})
    assert result.is_error


@pytest.mark.asyncio
async def test_search_pages_report_has_more(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    parent = await _create(sessionmaker, "родитель поиска")
    await _subtasks(sessionmaker, parent, "шаг 1", "шаг 2", "шаг 3")

    first = structured(
        await call_tool(
            mcp_server, "novryn_search_tasks", {"parent_task_id": str(parent), "limit": 2}
        )
    )
    assert len(first["items"]) == 2
    assert first["offset"] == 0
    assert first["has_more"] is True
    assert first["next_offset"] == 2

    rest = structured(
        await call_tool(
            mcp_server,
            "novryn_search_tasks",
            {"parent_task_id": str(parent), "limit": 2, "offset": 2},
        )
    )
    assert len(rest["items"]) == 1
    assert rest["has_more"] is False
    assert rest["next_offset"] is None
    assert len({t["id"] for t in first["items"] + rest["items"]}) == 3


@pytest.mark.asyncio
async def test_search_full_text_in_russian(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    parent = await _create(sessionmaker, "папка поиска")
    await _subtasks(sessionmaker, parent, "Подписать документы у нотариуса", "Купить молоко")
    found = structured(
        await call_tool(
            mcp_server,
            "novryn_search_tasks",
            {"q": "документ", "parent_task_id": str(parent)},
        )
    )
    assert [t["title"] for t in found["items"]] == ["Подписать документы у нотариуса"]


@pytest.mark.asyncio
async def test_search_rejects_time_without_timezone(mcp_server: MCPServer) -> None:
    result = await call_tool(
        mcp_server, "novryn_search_tasks", {"due_from": "2026-07-01T00:00:00"}
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_page_size_is_bounded(mcp_server: MCPServer) -> None:
    result = await call_tool(mcp_server, "novryn_search_tasks", {"limit": 101})
    assert result.is_error


@pytest.mark.asyncio
async def test_task_tree_is_nested(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    root = await _create(sessionmaker, "корень дерева")
    (branch,) = await _subtasks(sessionmaker, root, "ветка")
    await _subtasks(sessionmaker, branch, "лист")

    tree = structured(
        await call_tool(mcp_server, "novryn_get_task_tree", {"task_id": str(root)})
    )
    assert tree["id"] == str(root)
    assert [c["title"] for c in tree["children"]] == ["ветка"]
    assert [c["title"] for c in tree["children"][0]["children"]] == ["лист"]
    assert tree["children"][0]["children"][0]["children"] == []


@pytest.mark.asyncio
async def test_get_dependencies(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task = await _create(sessionmaker, "зависит")
    first = await _create(sessionmaker, "сначала это")
    second = await _create(sessionmaker, "и это")
    for blocker in (first, second):
        async with sessionmaker() as s:
            await dependency_service.link_tasks(
                s, task_id=task, depends_on_task_id=blocker, actor_type=_USER, actor_id=None
            )

    out = structured(
        await call_tool(mcp_server, "novryn_get_dependencies", {"task_id": str(task)})
    )
    assert out["task_id"] == str(task)
    # Порядок детерминирован: в порядке создания связей.
    assert [d["depends_on_task_id"] for d in out["dependencies"]] == [
        str(first),
        str(second),
    ]


@pytest.mark.asyncio
async def test_get_resources_lists_only_active(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task = await _create(sessionmaker, "со ссылками")
    ids = []
    for url in ("https://example.org/a", "https://example.org/b"):
        async with sessionmaker() as s:
            ids.append(
                await attachment_service.attach_resource(
                    s, task_id=task, type="URL", url=url, actor_type=_USER, actor_id=None
                )
            )
    async with sessionmaker() as s:
        await attachment_service.detach_resource(
            s, attachment_id=ids[0], actor_type=_USER, actor_id=None
        )

    out = structured(
        await call_tool(mcp_server, "novryn_get_resources", {"task_id": str(task)})
    )
    assert [(r["type"], r["url"]) for r in out["resources"]] == [
        ("URL", "https://example.org/b")
    ]


@pytest.mark.asyncio
async def test_get_sessions(
    mcp_server: MCPServer, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task = await _create(sessionmaker, "с сессиями")
    async with sessionmaker() as s:
        session_id = await session_service.start_session(
            s, task_id=task, planned_minutes=25, actor_type=_USER, actor_id=None
        )
    async with sessionmaker() as s:
        await session_service.finish_session(
            s,
            session_id=session_id,
            actual_minutes=30,
            result="COMPLETED",
            notes="успел",
            actor_type=_USER,
            actor_id=None,
        )

    out = structured(
        await call_tool(mcp_server, "novryn_get_sessions", {"task_id": str(task)})
    )
    (only,) = out["sessions"]
    assert only["id"] == str(session_id)
    assert (only["planned_minutes"], only["actual_minutes"]) == (25, 30)
    assert only["result"] == "COMPLETED"
    assert only["notes"] == "успел"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "novryn_get_task_tree",
        "novryn_get_dependencies",
        "novryn_get_resources",
        "novryn_get_sessions",
    ],
)
async def test_task_scoped_reads_reject_unknown_task(
    mcp_server: MCPServer, tool: str
) -> None:
    result = await call_tool(mcp_server, tool, {"task_id": str(new_id())})
    assert result.is_error
    assert "не найдена" in error_text(result)
