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
ПИШЕТ НОВЫЕ строки с новым общим ``focus_set_id`` (один UUID v7 на снимок) и
``generated_at`` — старые строки НЕ трогаются и НЕ удаляются (append-only,
T-03-05). Версия снимка идентифицируется ``focus_set_id``, НЕ ``generated_at``:
два снимка в пределах одного тика часов / при NTP-сдвиге назад дают равный
``generated_at`` и без отдельного ключа смешали бы строки версий (CR-01).
Чтение последней версии — задача ``focus_read`` (последний ``focus_set_id``).

``entity_id`` агрегированного события = ``focus_set_id`` снимка (стабильный
ключ всего снимка): связывает событие со всем набором строк версии, а
``payload_json`` ``{date, items}`` остаётся авторитетным по составу.
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
        ``entity_id`` записанного события (= ``focus_set_id`` снимка, стабильный
        ключ версии; осмыслен и для пустого снимка — WR-01).
    """
    generated_at = datetime.datetime.now(datetime.timezone.utc)
    # focus_set_id — непрозрачный ключ версии снимка (один UUID v7 на весь снимок,
    # общий для всех строк). Чтение последней версии выбирает по focus_set_id, а НЕ
    # по равенству generated_at: два снимка в одном тике / при NTP-сдвиге назад дают
    # равный generated_at, и WHERE generated_at == MAX(...) смешал бы их строки
    # (CR-01). focus_set_id уникален на снимок и устраняет коллизию версий.
    focus_set_id = new_id()
    rows: list[dict[str, Any]] = [
        {
            "id": new_id(),
            "date": date,
            "task_id": item["task_id"],
            "rank": item["rank"],
            "reason": item.get("reason"),
            "generated_by": generated_by,
            "focus_set_id": focus_set_id,
            "generated_at": generated_at,
        }
        for item in items
    ]
    # entity_id события = focus_set_id снимка (стабильный ключ всей версии).
    # Это исправляет WR-01: для ПУСТОГО снимка (items == []) раньше выдавался
    # суррогатный new_id(), не соответствующий ни одной строке — фантомный
    # entity_id, ломавший аудит-трассировку. focus_set_id осмыслен и для пустого
    # снимка: это легитимный факт «на эту дату фокуса нет» (Hermes-контракт),
    # привязанный к реальному ключу версии, а не к несуществующей строке.
    entity_id = focus_set_id

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
