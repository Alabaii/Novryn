"""Инструменты фокуса дня: список на дату и главная задача момента."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from novryn.db.models import DailyFocus, Task
from novryn.mcp.params import FocusDate
from novryn.mcp.runtime import Runtime
from novryn.mcp.schemas import Focus, FocusItem, FocusNow
from novryn.mcp.tools import read_tool
from novryn.repositories import focus_read, task_read


def _item(row: DailyFocus, task: Task) -> FocusItem:
    """Пункт фокуса вместе с названием и статусом задачи."""
    return FocusItem.model_validate(
        {
            "rank": row.rank,
            "task_id": row.task_id,
            "title": task.title,
            "status": task.status,
            "reason": row.reason,
        }
    )


def register(server: MCPServer, runtime: Runtime) -> None:
    """Зарегистрировать инструменты фокуса дня."""

    @read_tool(server, name="novryn_get_focus", title="Фокус дня")
    async def get_focus(date: FocusDate = None) -> Focus:
        """Фокус дня — список задач на дату, ранжированный Hermes (последняя версия
        снимка). В каждом пункте ранг, задача (название и статус) и причина выбора.
        Если на дату фокуса нет — ошибка."""
        day = date if date is not None else runtime.today()
        async with runtime.session() as session:
            rows = await focus_read.get_today_tasks(session, day)
            tasks = await task_read.get_tasks_by_ids(session, {r.task_id for r in rows})
            snapshot = rows[0]
            return Focus(
                date=day,
                focus_set_id=snapshot.focus_set_id,
                generated_at=snapshot.generated_at,
                generated_by=snapshot.generated_by,
                items=[_item(r, tasks[r.task_id]) for r in rows],
            )

    @read_tool(server, name="novryn_focus_now", title="Главная задача сейчас")
    async def focus_now(date: FocusDate = None) -> FocusNow:
        """Главная задача момента — первый пункт фокуса дня (ранг 1). Если на дату
        фокуса нет — ошибка."""
        day = date if date is not None else runtime.today()
        async with runtime.session() as session:
            row = await focus_read.focus_now(session, day)
            task = await task_read.require_task(session, row.task_id)
            return FocusNow(date=day, focus_set_id=row.focus_set_id, item=_item(row, task))
