"""Read-репозиторий Event Store: хронология событий одной сущности.

Только ЧТЕНИЕ (read-side). Запись в журнал — ``event_repository.append_event``;
чтение вынесено в отдельный модуль по принципу разделения append/query. Модуль узкий
и session-injected (как ``task_read``/``memory_read``): транзакцию НЕ открывает.
``entity_type``/``entity_id`` идут в SQL только bind-параметрами (V5).

История нужна Hermes, чтобы восстановить не только текущее состояние сущности, но и
путь к нему: каждое событие несёт полные снимки ``{before, after}`` (D-01/D-02).
``payload_json`` может содержать ``ai_context_json`` с персональными данными — модуль
отдаёт его авторизованному вызывающему, но логировать payload запрещено (T-01-16).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import Event

# Потолок размера страницы истории: даже limit>200 ограничивается этим (DoS-guard).
_HISTORY_LIMIT_CEILING = 200
_HISTORY_LIMIT_DEFAULT = 50


async def get_entity_history(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = _HISTORY_LIMIT_DEFAULT,
    offset: int = 0,
) -> list[Event]:
    """События одной сущности в порядке записи (от первого к последнему).

    ``entity_type`` обязателен: ``entity_id`` уникален внутри своей таблицы, но не
    между типами (например, ``entity_id`` события фокуса — это ``focus_set_id``), а
    ведущая колонка индекса ``idx_events_entity`` — именно ``entity_type``.

    Порядок: ``occurred_at``, затем ``id``. ``occurred_at`` ставит БД через
    ``now()`` — это время начала транзакции, поэтому события одной транзакции его
    делят; ``id`` (UUID v7, монотонный в процессе — NFR-01) упорядочивает их в
    порядке записи.

    Для неизвестной сущности возвращается пустой список — это не ошибка: пустую
    страницу даёт и ``offset`` за концом истории.

    Returns:
        Страница событий (``limit`` — по умолчанию 50, потолок 200; ``offset``).
    """
    stmt = (
        select(Event)
        .where(Event.entity_type == entity_type, Event.entity_id == entity_id)
        .order_by(Event.occurred_at, Event.id)
        .limit(min(limit, _HISTORY_LIMIT_CEILING))
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
