"""Перевод доменных ошибок на язык MCP: сообщение для модели и подсказка, что делать.

SDK передаёт текст ``ToolError`` модели как есть (``is_error=True``), а любое другое
исключение считает сбоем: модель видит только «Error executing tool <имя>», трейсбек
уходит в лог сервера. Поэтому сюда попадают только ожидаемые доменные ошибки — их
текст уже безопасен (id, даты, пределы) и написан для человека.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from novryn.domain.errors import DepthExceededError, DomainError, NotFoundError


def _hint(exc: DomainError) -> str | None:
    """Что модели сделать дальше, если это не очевидно из самого сообщения."""
    if isinstance(exc, NotFoundError) and exc.entity_type == "task":
        return "Проверьте id: найти задачу можно через novryn_search_tasks."
    if isinstance(exc, DepthExceededError):
        return (
            "Запросите дерево от одной из дочерних задач — их id даёт "
            "novryn_search_tasks с фильтром parent_task_id."
        )
    return None


def to_tool_error(exc: DomainError) -> ToolError:
    """Доменная ошибка → ``ToolError`` с понятным модели текстом."""
    hint = _hint(exc)
    return ToolError(f"{exc}. {hint}" if hint else str(exc))
