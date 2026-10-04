"""Инструменты знаний о пользователе: память, поведенческие паттерны и сводки."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from novryn.domain.errors import NotFoundError
from novryn.mcp.params import DEFAULT_LIMIT, Limit, MinConfidence, Offset, TaskId
from novryn.mcp.runtime import Runtime
from novryn.mcp.schemas import (
    MemoryOut,
    Page,
    PatternOut,
    Patterns,
    TaskInsights,
    UserInsights,
)
from novryn.mcp.tools import read_tool
from novryn.repositories import insights_read, memory_read, pattern_read


def register(server: MCPServer, runtime: Runtime) -> None:
    """Зарегистрировать инструменты памяти, паттернов и сводок."""

    @read_tool(server, name="novryn_search_memory", title="Поиск в памяти")
    async def search_memory(
        q: Annotated[
            str | None,
            Field(description="Полнотекстовый запрос по содержанию, с русской морфологией."),
        ] = None,
        memory_type: Annotated[
            str | None, Field(description="Только этот тип знания (точное совпадение).")
        ] = None,
        min_confidence: MinConfidence = None,
        source: Annotated[
            str | None, Field(description="Только из этого источника (точное совпадение).")
        ] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Page[MemoryOut]:
        """Долгосрочная память о пользователе: предпочтения, привычки, факты — по одной
        записи на тип знания. С запросом q — сначала самые релевантные, без него —
        сначала новые."""
        async with runtime.session() as session:
            rows = await memory_read.memory_search(
                session,
                q=q,
                memory_type=memory_type,
                min_confidence=min_confidence,
                source=source,
                limit=limit + 1,
                offset=offset,
            )
            return Page[MemoryOut].of(
                rows, limit=limit, offset=offset, convert=MemoryOut.model_validate
            )

    @read_tool(server, name="novryn_get_behavior_patterns", title="Поведенческие паттерны")
    async def get_behavior_patterns(
        pattern_type: Annotated[
            str | None, Field(description="Только этот тип паттерна (точное совпадение).")
        ] = None,
        min_confidence: MinConfidence = None,
    ) -> Patterns:
        """Поведенческие паттерны пользователя — наблюдения Hermes с уверенностью и
        доказательствами (evidence_json), от самых уверенных к менее уверенным."""
        async with runtime.session() as session:
            rows = await pattern_read.get_behavior_patterns(
                session, pattern_type=pattern_type, min_confidence=min_confidence
            )
            return Patterns(patterns=[PatternOut.model_validate(r) for r in rows])

    @read_tool(server, name="novryn_task_insights", title="Сводка по задаче")
    async def task_insights(task_id: TaskId) -> TaskInsights:
        """Сводка по задаче, посчитанная на лету: сессии (план, факт, итоги),
        появления в фокусе, число подзадач, активных зависимостей и вложений."""
        async with runtime.session() as session:
            insights = await insights_read.task_insights(session, task_id)
            if insights["status"] is None:  # задачи нет — сводка из нулей ввела бы в заблуждение
                raise NotFoundError(task_id)
            return TaskInsights.model_validate(insights)

    @read_tool(server, name="novryn_user_insights", title="Сводка по пользователю")
    async def user_insights() -> UserInsights:
        """Сводка по пользователю, посчитанная на лету: задачи по статусам и доля
        выполненных, отслеженное время, ведущие паттерны, объём памяти и активность
        фокуса."""
        async with runtime.session() as session:
            return UserInsights.model_validate(await insights_read.user_insights(session))
