"""Инструменты чтения задач: карточка, поиск, дерево и записи, привязанные к задаче."""

from __future__ import annotations

import uuid
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import AwareDatetime, Field

from novryn.domain.enums import TaskEnergy, TaskStatus
from novryn.mcp.params import DEFAULT_LIMIT, Limit, Offset, TaskId
from novryn.mcp.runtime import Runtime
from novryn.mcp.schemas import (
    AttachmentOut,
    DependencyOut,
    Page,
    SessionOut,
    TaskDependencies,
    TaskOut,
    TaskResources,
    TaskSessions,
    TaskTreeNode,
)
from novryn.mcp.tools import read_tool
from novryn.repositories import dependency_read, task_read
from novryn.services import hierarchy_service


def register(server: MCPServer, runtime: Runtime) -> None:
    """Зарегистрировать инструменты задач."""

    @read_tool(server, name="novryn_get_task", title="Задача по id")
    async def get_task(task_id: TaskId) -> TaskOut:
        """Полная карточка задачи: статус, срок, оценки времени, требуемая энергия,
        причина блокировки и свободный контекст Hermes (ai_context_json)."""
        async with runtime.session() as session:
            return TaskOut.model_validate(await task_read.require_task(session, task_id))

    @read_tool(server, name="novryn_search_tasks", title="Поиск задач")
    async def search_tasks(
        q: Annotated[
            str | None,
            Field(
                description="Полнотекстовый запрос по названию и описанию, с русской "
                "морфологией: «документ» найдёт «документы»."
            ),
        ] = None,
        status: Annotated[
            TaskStatus | None, Field(description="Только этот статус (сильнее blocked).")
        ] = None,
        parent_task_id: Annotated[
            uuid.UUID | None, Field(description="Только прямые подзадачи этой задачи.")
        ] = None,
        blocked: Annotated[
            bool | None,
            Field(description="true — только заблокированные, false — все, кроме них."),
        ] = None,
        energy: Annotated[
            TaskEnergy | None, Field(description="Требуемая энергия.")
        ] = None,
        due_from: Annotated[
            AwareDatetime | None,
            Field(description="Срок не раньше (ISO 8601 с часовым поясом)."),
        ] = None,
        due_to: Annotated[
            AwareDatetime | None,
            Field(description="Срок не позже (ISO 8601 с часовым поясом)."),
        ] = None,
        include_archived: Annotated[
            bool, Field(description="Показать и архивные (по умолчанию скрыты).")
        ] = False,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Page[TaskOut]:
        """Найти задачи по тексту и фильтрам; фильтры комбинируются. С запросом q —
        сначала самые релевантные, без него — сначала новые. Архивные скрыты,
        пока не задан status=ARCHIVED или include_archived."""
        async with runtime.session() as session:
            rows = await task_read.search_tasks(
                session,
                q=q,
                status=status,
                parent_task_id=parent_task_id,
                blocked=blocked,
                energy=energy,
                due_from=due_from,
                due_to=due_to,
                include_archived=include_archived,
                limit=limit + 1,
                offset=offset,
            )
            return Page[TaskOut].of(
                rows, limit=limit, offset=offset, convert=TaskOut.model_validate
            )

    @read_tool(server, name="novryn_get_task_tree", title="Дерево задачи")
    async def get_task_tree(task_id: TaskId) -> TaskTreeNode:
        """Задача и все её подзадачи на всю глубину, включая архивные: id, название,
        статус и вложенные children."""
        async with runtime.session() as session:
            tree = await hierarchy_service.get_task_tree(session, task_id)
            return TaskTreeNode.model_validate(tree)

    @read_tool(server, name="novryn_get_dependencies", title="Зависимости задачи")
    async def get_dependencies(task_id: TaskId) -> TaskDependencies:
        """Задачи, которые нужно сделать раньше этой (активные зависимости)."""
        async with runtime.session() as session:
            await task_read.require_task(session, task_id)
            rows = await dependency_read.get_dependencies(session, task_id)
            return TaskDependencies(
                task_id=task_id,
                dependencies=[DependencyOut.model_validate(r) for r in rows],
            )

    @read_tool(server, name="novryn_get_resources", title="Вложения задачи")
    async def get_resources(task_id: TaskId) -> TaskResources:
        """Внешние ресурсы задачи: ссылки, документы, PDF, репозитории (только активные)."""
        async with runtime.session() as session:
            await task_read.require_task(session, task_id)
            rows = await dependency_read.get_resources(session, task_id)
            return TaskResources(
                task_id=task_id, resources=[AttachmentOut.model_validate(r) for r in rows]
            )

    @read_tool(server, name="novryn_get_sessions", title="Сессии задачи")
    async def get_sessions(task_id: TaskId) -> TaskSessions:
        """Все попытки поработать над задачей, от старых к новым: план и факт в минутах,
        итог и заметки."""
        async with runtime.session() as session:
            await task_read.require_task(session, task_id)
            rows = await dependency_read.get_sessions(session, task_id)
            return TaskSessions(
                task_id=task_id, sessions=[SessionOut.model_validate(r) for r in rows]
            )
