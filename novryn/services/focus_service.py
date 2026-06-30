"""Сервис daily focus: запись снимка фокуса Hermes на день (FOCUS-01/D-03).

ОСОБЫЙ СЛУЧАЙ аудита (D-03). Обычные мутации идут через UoW
(``mutate_with_event`` — одно событие на строку, ``mutate_many_with_event`` —
событие НА КАЖДУЮ строку батча). Снимок фокуса другой: N строк ``daily_focus``
пишутся одним bulk-INSERT, но порождают РОВНО ОДНО агрегированное событие
``focus.generated`` на весь снимок — а не событие-на-строку. Поэтому
``mutate_many_with_event`` здесь НЕ подходит (он нарушил бы D-03).

Это НЕ обход аудит-инварианта (NFR-06): bulk-INSERT и ``append_event``
выполняются в ОДНОЙ ``async with session.begin()`` — сбой отката оставляет
0 строк и 0 событий. Гранулярность события = снимок (а не строка): фокус —
append-only журнал решений, не правка существующих строк, поэтому
построчные before/after-снимки здесь не нужны; авторитетный состав снимка
несёт ``payload_json`` события.

Версионирование (D-01/D-02): повторный ``generate_daily_focus`` на ту же дату
ПИШЕТ НОВЫЕ строки с новым общим ``generated_at`` — старые строки НЕ трогаются
и НЕ удаляются (append-only, T-03-05). Чтение последней версии — задача
``focus_read`` (подзапрос ``MAX(generated_at)``).

``entity_id`` агрегированного события = id первой строки снимка (T-03-07,
сознательно принято для single-user V1): при механизме ``generated_at`` без
отдельного ``focus_set_id`` групповая привязка держится через ``payload_json``
``{date, items}``, который авторитетен по составу снимка.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import DailyFocus
from novryn.domain.events import ActorType, EventType
from novryn.repositories.event_repository import append_event


async def generate_daily_focus(
    session: AsyncSession,
    *,
    date: datetime.date,
    items: list[dict[str, Any]],
    generated_by: str | None = None,
) -> uuid.UUID:
    """Записать снимок фокуса: N строк daily_focus + ОДНО focus.generated (D-03).

    Все N строк делят ОБЩИЙ ``generated_at`` (момент батча — версия снимка,
    D-02); каждая строка получает свой ``id=new_id()`` (UUID v7, NFR-01).
    Bulk-INSERT и ``append_event`` идут в ОДНОЙ локальной транзакции
    (``async with session.begin()``) — атомарность мутации и события (NFR-06):
    любой сбой откатывает оба, не оставляя ни строк, ни orphan-события.

    Повторный вызов на ту же дату НЕ трогает прежние строки — пишет новую
    версию (D-01); чтение последней версии — ``focus_read.get_today_tasks``.
    Число ``items`` не ограничивается (FOCUS-03). ``generated_by`` (источник
    решения, например "hermes") сохраняется в каждой строке снимка как есть.

    ``payload_json`` события — агрегат ``{"date": <ISO>, "items": [...]}``
    (НЕ построчный ``serialize_row``): для append-only журнала фокуса нужен
    состав снимка, а не before/after отдельной строки.

    Args:
        session: свежая async-сессия (одна на операцию; Anti-Pattern 5).
        date: дата, на которую формируется снимок фокуса.
        items: список ``{"task_id", "rank", "reason"}`` — задачи снимка.
        generated_by: источник решения (пишется в строки daily_focus).

    Returns:
        ``entity_id`` записанного события (id первой строки снимка, T-03-07).
    """
    generated_at = datetime.datetime.now(datetime.timezone.utc)
    rows: list[dict[str, Any]] = [
        {
            "id": new_id(),
            "date": date,
            "task_id": item["task_id"],
            "rank": item["rank"],
            "reason": item.get("reason"),
            "generated_by": generated_by,
            "generated_at": generated_at,
        }
        for item in items
    ]
    # entity_id события = id первой строки снимка (T-03-07). Для пустого снимка
    # отдельный id, чтобы событие всё равно имело валидный entity_id.
    entity_id = rows[0]["id"] if rows else new_id()

    payload_items = [
        {
            "task_id": str(item["task_id"]),
            "rank": item["rank"],
            "reason": item.get("reason"),
        }
        for item in items
    ]

    async with session.begin():
        if rows:
            await session.execute(insert(DailyFocus), rows)
        await append_event(
            session,
            event_type=EventType.FOCUS_GENERATED,
            entity_type="daily_focus",
            entity_id=entity_id,
            actor_type=ActorType.HERMES,
            actor_id=None,
            payload_json={
                "date": date.isoformat(),
                "generated_by": generated_by,
                "items": payload_items,
            },
        )
    return entity_id
