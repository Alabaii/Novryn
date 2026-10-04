"""Инструмент истории: путь сущности к текущему состоянию из журнала событий."""

from __future__ import annotations

import uuid
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from novryn.mcp.params import DEFAULT_LIMIT, EntityType, Limit, Offset
from novryn.mcp.runtime import Runtime
from novryn.mcp.schemas import EventOut, Page
from novryn.mcp.tools import read_tool
from novryn.repositories import event_read


def register(server: MCPServer, runtime: Runtime) -> None:
    """Зарегистрировать инструмент истории сущности."""

    @read_tool(server, name="novryn_get_entity_history", title="История сущности")
    async def get_entity_history(
        entity_type: Annotated[EntityType, Field(description="Тип сущности.")],
        entity_id: Annotated[uuid.UUID, Field(description="id сущности (UUID).")],
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ) -> Page[EventOut]:
        """История сущности из журнала событий, от первого события к последнему: что
        произошло, кто это сделал (USER, HERMES или SYSTEM) и полные снимки до и после.
        Журнал только дополняется — исправление прошлого видно новым событием."""
        async with runtime.session() as session:
            rows = await event_read.get_entity_history(
                session,
                entity_type=entity_type,
                entity_id=entity_id,
                limit=limit + 1,
                offset=offset,
            )
            return Page[EventOut].of(
                rows, limit=limit, offset=offset, convert=EventOut.model_validate
            )
