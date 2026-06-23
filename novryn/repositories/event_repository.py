"""Event Store: append-only запись событий (NFR-06 / EVT-03).

НАМЕРЕННО узкий API: единственная операция — ``append_event`` (INSERT в events).
Методов update/delete/get-for-mutation НЕТ — изменить событие нельзя по дизайну
КОДА (рубеж в дополнение к БД-триггеру `trg_events_append_only` и REVOKE под
novryn_app из миграции 001, план 03). Это структурный рубеж против T-01-14
(подмена события через код приложения).

``append_event`` НЕ открывает транзакцию и НЕ коммитит: вызывается ВНУТРИ
``session.begin()`` из UoW (novryn/repositories/uow.py), чтобы событие и доменная
мутация коммитились вместе (D-04). Прямой вызов после ``session.commit()``
запрещён (Anti-Pattern 1 — аудит-гэп).

Чтение (аудит-запросы) появится в будущих фазах отдельным read-only API — оно
вне Фазы 1 и вне этого модуля по принципу разделения append/query.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import Event


async def append_event(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
    payload_json: dict[str, Any],
    schema_version: int = 1,
) -> uuid.UUID:
    """Записать одно событие в Event Store (только INSERT).

    ``id`` генерируется через ``new_id`` (UUID v7, NFR-01); ``occurred_at`` ставит
    БД (``server_default=now()``) — не передаётся, чтобы время фиксировала одна
    точка (БД). Поля ``event_type``/``actor_type`` задаёт сервис явно (D-05);
    форма ``payload_json`` — ``{"before": ..., "after": ...}`` (D-01), её собирает
    UoW единым сериализатором.

    Вызывать ТОЛЬКО внутри открытой транзакции (``session.begin()`` в UoW): сам
    по себе append_event не коммитит. Возвращает сгенерированный ``id`` события.

    Args:
        session: активная async-сессия внутри транзакции мутации.
        event_type: бизнес-смысловое имя (см. domain/events.EventType), явно (D-05).
        entity_type: тип сущности ('task', 'session', ...).
        entity_id: id затронутой строки сущности.
        actor_type: 'USER' | 'HERMES' | 'SYSTEM' (см. domain/events.ActorType).
        actor_id: id актора или None (SYSTEM/seed).
        payload_json: снимки {'before': ..., 'after': ...} (D-01/D-02/D-03).
        schema_version: версия схемы события (EVT-04), по умолчанию 1.

    Returns:
        UUID v7 созданного события.
    """
    event_id = new_id()
    await session.execute(
        insert(Event).values(
            id=event_id,
            schema_version=schema_version,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload_json=payload_json,
        )
    )
    return event_id
