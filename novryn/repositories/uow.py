"""Unit-of-Work: атомарная со-запись мутации и события (D-04, EVT-01, NFR-06).

ЯДРО аудит-инварианта и КОНТРАКТ для Фазы 2+. Каждая доменная мутация проходит
через ``mutate_with_event``: внутри ОДНОГО ``async with session.begin()``
собираются снимки `before`/`after` (полная строка сущности) и в ТОЙ ЖЕ
транзакции пишется событие. Коммит один — мутация и событие сохраняются вместе
или не сохраняются вовсе. Записать мутацию без события (или событие без мутации)
структурно невозможно (критерий #3).

Контракт стабилен — на нём доменные сервисы Фазы 2+ строят все операции:
сервис задаёт ``event_type``/``actor`` ЯВНО по бизнес-смыслу (D-05), а слой
собирает снимки автоматически. ``event_type`` НЕ выводится из dirty-трекинга
SQLAlchemy.

Почему снимок собирается ЯВНО (через ``load_row``), а НЕ через SQLAlchemy
``before_flush``: в async-контексте listener получает синхронный Session-прокси и
НЕ может ``await`` (→ ``MissingGreenlet``, 01-RESEARCH.md Pattern 1); к тому же
D-05 требует явного ``event_type`` от сервиса, что естественно живёт в UoW, а не
в обезличенном глобальном listener'е.

Каждый вызов UoW должен получать СВЕЖУЮ короткоживущую ``AsyncSession`` (одна на
доменную операцию) — долгоживущая/разделяемая между конкурентными вызовами
сессия запрещена (Anti-Pattern 5 / Pitfall 3).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from novryn.db.serialization import serialize_row
from novryn.repositories.event_repository import append_event


async def _ensure_loaded(session: AsyncSession, row: object) -> None:
    """Гарантировать, что атрибуты ORM-строки загружены ВНУТРИ async-контекста.

    Если ``row`` — ORM-инстанс с истёкшими (expired) атрибутами (после
    apply+flush), синхронный ``getattr`` в ``serialize_row`` спровоцировал бы
    ленивую IO вне greenlet → ``MissingGreenlet``. Явный ``session.refresh``
    дозагружает их здесь, в awaitable-контексте. Для ``Mapping`` — no-op
    (значения уже материализованы, ленивой загрузки нет).
    """
    if isinstance(row, Mapping):
        return
    mapper: Mapper[Any] | None = inspect(type(row), raiseerr=False)
    if mapper is not None and isinstance(mapper, Mapper):
        await session.refresh(row)

# Доменная мутация: применяет INSERT/UPDATE и возвращает произвольный результат.
ApplyFn = Callable[[AsyncSession], Awaitable[Any]]
# Загрузка полной строки сущности (ORM-инстанс или Mapping) либо None (нет строки).
LoadRowFn = Callable[[AsyncSession], Awaitable[object | None]]


async def mutate_with_event(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    apply: ApplyFn,
    load_row: LoadRowFn,
    schema_version: int = 1,
) -> Any:
    """Применить доменную мутацию и атомарно записать её событие.

    Последовательность внутри ЕДИНСТВЕННОГО ``session.begin()`` (D-04):
      1. ``before = await load_row(session)`` — полная строка ДО (None при CREATE);
      2. ``result = await apply(session)``    — доменный INSERT/UPDATE;
      3. ``await session.flush()``            — материализовать server-default'ы
         (например ``created_at``) ДО снимка `after`;
      4. ``after = await load_row(session)``  — полная строка ПОСЛЕ;
      5. сериализовать оба снимка единым ``serialize_row`` (одинаковая форма);
      6. ``append_event(..., payload_json={'before': before, 'after': after})``.
    Транзакция коммитится при выходе из ``async with``. Любое исключение на шагах
    1–6 (в т.ч. сбой INSERT события) откатывает ВСЮ транзакцию — ни доменной
    строки, ни события не остаётся (критерий #3, EVT-01, NFR-06).

    Снимки несут ПОЛНУЮ строку сущности (D-02), включая тяжёлое ``ai_context_json``
    целиком (D-03). На CREATE ``before`` = None.

    Args:
        session: свежая async-сессия (одна на операцию; Anti-Pattern 5).
        entity_type: тип сущности события ('task', 'session', ...).
        entity_id: id затронутой строки.
        event_type: бизнес-смысловое имя события, ЯВНО от сервиса (D-05).
        actor_type: 'USER' | 'HERMES' | 'SYSTEM' (D-05).
        actor_id: id актора или None (SYSTEM/seed).
        apply: awaitable доменной мутации; его результат возвращается наружу.
        load_row: awaitable, возвращающий полную строку сущности или None.
        schema_version: версия схемы события (EVT-04), по умолчанию 1.

    Returns:
        Значение, возвращённое ``apply`` (например созданная/обновлённая сущность).
    """
    async with session.begin():
        before_row = await load_row(session)
        # Сериализуем before СРАЗУ, пока строка свежая: последующий apply (UPDATE)
        # + flush истекает (expire) атрибуты того же ORM-инстанса, и отложенный
        # serialize_row(before_row) дёрнул бы ленивую дозагрузку вне greenlet →
        # MissingGreenlet. Материализуем значения в plain dict здесь и сейчас.
        before = serialize_row(before_row) if before_row is not None else None
        if before_row is not None:
            # CR-02: отвязать identity-map-инстанс before от сессии. На UPDATE
            # ``load_row`` (session.get) вернул бы ТОТ ЖЕ объект для after; expunge
            # гарантирует, что after_row будет СВЕЖЕЙ загрузкой, а before_row уже
            # нельзя задним числом «обновить» через flush/refresh. Снимок before
            # структурно не может схлопнуться в after через identity-map — даже
            # если сериализацию before когда-нибудь переставят после apply
            # (defense-in-depth поверх раннего serialize_row выше). Защита ядра
            # аудит-инварианта: before != after на изменённых полях.
            session.expunge(before_row)

        result = await apply(session)
        # flush до снимка after: server-default'ы (created_at/updated_at) должны
        # быть материализованы, иначе after-снимок их не увидит.
        await session.flush()
        after_row = await load_row(session)
        if after_row is not None:
            # apply (UPDATE) истёк атрибуты identity-map-инстанса; явный async
            # refresh дозагружает их ВНУТРИ greenlet, иначе sync-getattr в
            # serialize_row дёрнул бы ленивую IO вне greenlet → MissingGreenlet.
            await _ensure_loaded(session, after_row)
        after = serialize_row(after_row) if after_row is not None else None

        await append_event(
            session,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload_json={"before": before, "after": after},
            schema_version=schema_version,
        )
    return result
