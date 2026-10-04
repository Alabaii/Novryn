"""Инструменты MCP-сервера, сгруппированные по доменам.

Каждый модуль экспортирует ``register(server, runtime)`` и регистрирует свои
инструменты замыканиями над ``Runtime`` — без глобального состояния.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

ToolFn = TypeVar("ToolFn", bound=Callable[..., Any])

# Подсказка клиенту: инструмент ничего не меняет, повтор безопасен, мир вне Novryn
# не затрагивается. Подсказка, а не защита: права проверяет сервер.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def describe(fn: Callable[..., Any]) -> str:
    """Описание инструмента из docstring: строки абзаца склеены, отступы убраны.

    SDK берёт docstring как есть — вместе с переносами и отступами исходника.
    """
    paragraphs = inspect.cleandoc(fn.__doc__ or "").split("\n\n")
    return "\n\n".join(" ".join(p.split()) for p in paragraphs)


def read_tool(server: MCPServer, *, name: str, title: str) -> Callable[[ToolFn], ToolFn]:
    """Декоратор: зарегистрировать инструмент чтения (аннотация read-only, описание)."""

    def decorate(fn: ToolFn) -> ToolFn:
        server.tool(
            name=name, title=title, description=describe(fn), annotations=_READ_ONLY
        )(fn)
        return fn

    return decorate
