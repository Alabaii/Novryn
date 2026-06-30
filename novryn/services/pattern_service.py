"""Сервис поведенческих паттернов (PRD §11; PAT-01/02/03).

Паттерн = текущий срез наблюдения Hermes: РОВНО одна актуальная строка на
``pattern_type`` (UNIQUE-ключ ``uq_behavior_pattern_type``, введён миграцией 003).
Запись — upsert по ``pattern_type`` (симметрия с памятью, D-05/D-06): строки нет →
create (``pattern.stored``), строка есть → in-place UPDATE (``pattern.updated``).
Семантика события выбирается ЯВНО пред-проверкой существования (зеркало
``memory_store``/``update_task``), а НЕ из исхода ON CONFLICT/dirty-трекинга (D-07):
UNIQUE — лишь страховка от гонки, не источник бизнес-смысла события.

TOCTOU (WR-02, ПРИНЯТЫЙ ОГРАНИЧЕННЫЙ РИСК для single-user V1): между
пред-проверкой и INSERT нет блокировки строки/типа. Два КОНКУРЕНТНЫХ
``pattern_store`` с одним ``pattern_type``, оба не нашедшие строку, оба пойдут
по ветке INSERT — второй упадёт на ``uq_behavior_pattern_type`` (IntegrityError),
а не выполнит update. В single-user V1 параллельных писателей нет, риск принят
сознательно. Долгосрочно (V2): ``INSERT ... ON CONFLICT (pattern_type) DO
UPDATE`` с выбором ``event_type`` по ``xmax``, либо advisory-lock на
``pattern_type`` в той же транзакции, что и запись.

confidence-валидация — на уровне БД (CHECK ``ck_behavior_confidence``, D-08):
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
from novryn.db.models import BehaviorPattern
from novryn.domain.events import ActorType, EventType
from novryn.repositories.uow import mutate_with_event


async def pattern_store(
    session: AsyncSession,
    *,
    pattern_type: str,
    confidence: float,
    evidence_json: dict[str, object],
    actor_type: str = ActorType.HERMES,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Upsert паттерна по ``pattern_type`` + ЯВНОЕ событие stored/updated (PAT-01/03).

    Пред-проверка существования строки выбирает event_type ЯВНО (D-07):
    нет строки → ``pattern.stored`` (create, before=None); есть → ``pattern.updated``
    (in-place UPDATE той же строки). После пред-проверки ``session.rollback()``
    закрывает read-autobegin, чтобы UoW открыл свою явную ``session.begin()``
    (иначе "transaction already begun" — Pitfall 2).

    На UPDATE ``updated_at=func.now()`` ставится ЯВНО (Pitfall 3): ORM ``onupdate``
    срабатывает не для всех Core-UPDATE путей, а PAT-03 требует продвижения метки.

    confidence вне 0.0–1.0 отвергается БД-CHECK на write как ``IntegrityError``
    (D-08/Pitfall 4) — Python-проверки диапазона здесь НЕТ.

    Returns:
        id строки паттерна: новый id при create, существующий id при update.
    """
    existing = (
        await session.execute(
            select(BehaviorPattern).where(
                BehaviorPattern.pattern_type == pattern_type
            )
        )
    ).scalar_one_or_none()
    # Материализуем нужный id ДО rollback: rollback истекает (expire) ORM-инстанс
    # existing, и последующий sync-доступ existing.id дёрнул бы ленивую IO вне
    # greenlet → MissingGreenlet. Берём значение сейчас, пока строка свежая.
    existing_id = existing.id if existing is not None else None

    # Пред-проверка (session.execute выше) авто-начала read-транзакцию; закрываем
    # её, чтобы UoW открыл свою явную session.begin() (Pitfall 2). Read-only —
    # откатывать нечего; UoW перезагрузит before свежим (load_row).
    #
    # WR-03: rollback() безусловно сотрёт ЛЮБЫЕ незакоммиченные изменения сессии.
    # Контракт «свежая сессия» (docstring) держится на дисциплине вызывающего;
    # проверяем его ЯВНО, чтобы нарушение падало громко, а не приводило к тихой
    # потере данных (если будущая composite-операция передаст «грязную» сессию).
    if session.new or session.dirty or session.deleted:
        raise RuntimeError(
            "pattern_store требует свежую сессию без незакоммиченных изменений "
            "(session.new/dirty/deleted должны быть пусты до пред-проверки); "
            "rollback() иначе тихо сотрёт их (WR-03)."
        )
    await session.rollback()

    if existing_id is None:
        entity_id = new_id()
        event_type = EventType.PATTERN_STORED

        async def apply(s: AsyncSession) -> uuid.UUID:
            await s.execute(
                insert(BehaviorPattern).values(
                    id=entity_id,
                    pattern_type=pattern_type,
                    confidence=confidence,
                    evidence_json=evidence_json,
                )
            )
            return entity_id
    else:
        entity_id = existing_id
        event_type = EventType.PATTERN_UPDATED

        async def apply(s: AsyncSession) -> uuid.UUID:
            await s.execute(
                update(BehaviorPattern)
                .where(BehaviorPattern.id == entity_id)
                .values(
                    confidence=confidence,
                    evidence_json=evidence_json,
                    updated_at=func.now(),  # ЯВНО — PAT-03/Pitfall 3
                )
            )
            return entity_id

    await mutate_with_event(
        session,
        entity_type="behavior_patterns",
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(BehaviorPattern, entity_id),
    )
    return entity_id
