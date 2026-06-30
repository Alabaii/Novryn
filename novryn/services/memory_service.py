"""Сервис долгосрочной памяти о пользователе (PRD §10; MEM-01/02/04).

Память = текущий срез убеждений Hermes: РОВНО одна актуальная строка на
``memory_type`` (UNIQUE-ключ ``uq_user_memory_type``, введён миграцией 003).
Запись — upsert по ``memory_type``: строки нет → create (``memory.stored``),
строка есть → in-place UPDATE (``memory.updated``). Семантика события выбирается
ЯВНО пред-проверкой существования (зеркало ``update_task``), а НЕ из исхода
ON CONFLICT/dirty-трекинга (D-07): UNIQUE — лишь страховка от гонки, не источник
бизнес-смысла события.

confidence-валидация — на уровне БД (CHECK ``ck_user_memory_confidence``, D-08):
значение вне 0.0–1.0 отвергается на write через ``IntegrityError``. Сервис
дублирующей Python-проверки НЕ вводит и CHECK не ослабляет.

Каждая мутация проходит через UoW Фазы 1 (``mutate_with_event``) — строка и её
событие пишутся в ОДНОЙ транзакции (NFR-06). Сервис получает СВЕЖУЮ
``AsyncSession`` и НИКОГДА не открывает свою транзакцию (это делает UoW).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import UserMemory
from novryn.domain.events import ActorType, EventType
from novryn.repositories.uow import mutate_with_event


async def memory_store(
    session: AsyncSession,
    *,
    memory_type: str,
    content: str,
    confidence: float,
    source: str,
    actor_type: str = ActorType.HERMES,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Upsert памяти по ``memory_type`` + ЯВНОЕ событие stored/updated (MEM-01/04).

    Пред-проверка существования строки выбирает event_type ЯВНО (D-07):
    нет строки → ``memory.stored`` (create, before=None); есть → ``memory.updated``
    (in-place UPDATE той же строки). После пред-проверки ``session.rollback()``
    закрывает read-autobegin, чтобы UoW открыл свою явную ``session.begin()``
    (иначе "transaction already begun" — Pitfall 2).

    На UPDATE ``updated_at=func.now()`` ставится ЯВНО (Pitfall 3): ORM ``onupdate``
    срабатывает не для всех Core-UPDATE путей, а MEM-04 требует продвижения метки.

    confidence вне 0.0–1.0 отвергается БД-CHECK на write как ``IntegrityError``
    (D-08/Pitfall 4) — Python-проверки диапазона здесь НЕТ.

    Returns:
        id строки памяти: новый id при create, существующий id при update.
    """
    existing = (
        await session.execute(
            select(UserMemory).where(UserMemory.memory_type == memory_type)
        )
    ).scalar_one_or_none()
    # Материализуем нужный id ДО rollback: rollback истекает (expire) ORM-инстанс
    # existing, и последующий sync-доступ existing.id дёрнул бы ленивую IO вне
    # greenlet → MissingGreenlet. Берём значение сейчас, пока строка свежая.
    existing_id = existing.id if existing is not None else None

    # Пред-проверка (session.execute выше) авто-начала read-транзакцию; закрываем
    # её, чтобы UoW открыл свою явную session.begin() (Pitfall 2). Read-only —
    # откатывать нечего; UoW перезагрузит before свежим (load_row).
    await session.rollback()

    if existing_id is None:
        entity_id = new_id()
        event_type = EventType.MEMORY_STORED

        async def apply(s: AsyncSession) -> uuid.UUID:
            await s.execute(
                insert(UserMemory).values(
                    id=entity_id,
                    memory_type=memory_type,
                    content=content,
                    confidence=confidence,
                    source=source,
                )
            )
            return entity_id
    else:
        entity_id = existing_id
        event_type = EventType.MEMORY_UPDATED

        async def apply(s: AsyncSession) -> uuid.UUID:
            await s.execute(
                update(UserMemory)
                .where(UserMemory.id == entity_id)
                .values(
                    content=content,
                    confidence=confidence,
                    source=source,
                    updated_at=func.now(),  # ЯВНО — MEM-04/Pitfall 3
                )
            )
            return entity_id

    await mutate_with_event(
        session,
        entity_type="user_memory",
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(UserMemory, entity_id),
    )
    return entity_id
