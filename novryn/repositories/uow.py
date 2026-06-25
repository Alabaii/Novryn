"""Unit-of-Work: атомарная со-запись мутации и события (D-04, EVT-01, NFR-06).

ЯДРО аудит-инварианта и КОНТРАКТ для Фазы 2+. Каждая доменная мутация проходит
через ``mutate_with_event`` (одиночная) или ``mutate_many_with_event`` (батч):
внутри ОДНОГО ``async with session.begin()`` для каждой сущности собираются снимки
`before`/`after` (полная строка) и в ТОЙ ЖЕ транзакции пишется событие. Коммит
один — мутации и события сохраняются вместе или не сохраняются вовсе. Записать
мутацию без события (или событие без мутации) структурно невозможно (критерий #3).

Обе функции делят единый per-item шаг ``_apply_one`` — единственный источник
истины по логике снимков (before → serialize → expunge → apply → flush → after →
serialize → append_event). ``_apply_one`` транзакцию НЕ открывает: её открывает
вызывающая функция (одну на одиночную мутацию; одну на ВЕСЬ батч — D-03
all-or-nothing: сбой на k-м элементе откатывает все предыдущие).

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
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from novryn.db.serialization import serialize_row
from novryn.repositories.event_repository import append_event

# Доменная мутация: применяет INSERT/UPDATE и возвращает произвольный результат.
ApplyFn = Callable[[AsyncSession], Awaitable[Any]]
# Загрузка полной строки сущности (ORM-инстанс или Mapping) либо None (нет строки).
LoadRowFn = Callable[[AsyncSession], Awaitable[object | None]]


@dataclass
class MutationSpec:
    """Спецификация одной доменной мутации для UoW (D-05).

    Несёт всё, что нужно ``_apply_one`` для применения мутации и атомарной записи
    её события: бизнес-смысловой ``event_type``/``actor`` (ЯВНО от сервиса, D-05),
    ``apply`` (INSERT/UPDATE) и ``load_row`` (полная строка для снимков
    before/after). Используется и одиночным ``mutate_with_event``, и батчевым
    ``mutate_many_with_event`` — единая форма описания мутации.
    """

    entity_type: str
    entity_id: uuid.UUID
    event_type: str
    actor_type: str
    actor_id: uuid.UUID | None
    apply: ApplyFn
    load_row: LoadRowFn
    schema_version: int = 1


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


async def _apply_one(session: AsyncSession, spec: MutationSpec) -> Any:
    """Применить ОДНУ мутацию + записать её событие. Транзакцию НЕ открывает.

    Единственный источник истины по логике снимков. Выполняется ВНУТРИ
    caller-provided ``session.begin()`` (одиночный ``mutate_with_event`` или
    батчевый ``mutate_many_with_event``). Последовательность (D-04):
      1. ``before = await spec.load_row(session)`` — полная строка ДО (None при CREATE);
      2. сериализовать before СРАЗУ (пока строка свежая);
      3. ``expunge(before_row)`` (CR-02) — отвязать от identity-map;
      4. ``result = await spec.apply(session)`` — доменный INSERT/UPDATE;
      5. ``await session.flush()`` — материализовать server-default'ы до снимка after;
      6. ``after = await spec.load_row(session)`` + ``_ensure_loaded`` — полная строка ПОСЛЕ;
      7. ``append_event(..., payload_json={'before': before, 'after': after})``.
    Снимки несут ПОЛНУЮ строку сущности (D-02), включая ``ai_context_json``
    целиком (D-03). На CREATE ``before`` = None. Любое исключение пробрасывается
    наверх и откатывает транзакцию вызывающей функции.
    """
    before_row = await spec.load_row(session)
    # Сериализуем before СРАЗУ, пока строка свежая: последующий apply (UPDATE)
    # + flush истекает (expire) атрибуты того же ORM-инстанса, и отложенный
    # serialize_row(before_row) дёрнул бы ленивую дозагрузку вне greenlet →
    # MissingGreenlet. Материализуем значения в plain dict здесь и сейчас.
    before = serialize_row(before_row) if before_row is not None else None
    if before_row is not None:
        # CR-02: отвязать identity-map-инстанс before от сессии. На UPDATE
        # load_row (session.get) вернул бы ТОТ ЖЕ объект для after; expunge
        # гарантирует, что after_row будет СВЕЖЕЙ загрузкой, а before_row уже
        # нельзя задним числом «обновить» через flush/refresh. Снимок before
        # структурно не может схлопнуться в after через identity-map. Защита
        # ядра аудит-инварианта: before != after на изменённых полях.
        session.expunge(before_row)

    result = await spec.apply(session)
    # flush до снимка after: server-default'ы (created_at/updated_at) должны
    # быть материализованы, иначе after-снимок их не увидит.
    await session.flush()
    after_row = await spec.load_row(session)
    if after_row is not None:
        # apply (UPDATE) истёк атрибуты identity-map-инстанса; явный async
        # refresh дозагружает их ВНУТРИ greenlet, иначе sync-getattr в
        # serialize_row дёрнул бы ленивую IO вне greenlet → MissingGreenlet.
        await _ensure_loaded(session, after_row)
    after = serialize_row(after_row) if after_row is not None else None

    await append_event(
        session,
        event_type=spec.event_type,
        entity_type=spec.entity_type,
        entity_id=spec.entity_id,
        actor_type=spec.actor_type,
        actor_id=spec.actor_id,
        payload_json={"before": before, "after": after},
        schema_version=spec.schema_version,
    )
    return result


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
    """Применить ОДНУ доменную мутацию и атомарно записать её событие.

    Открывает единственный ``session.begin()`` и делегирует per-item логику
    ``_apply_one`` (before → serialize → expunge → apply → flush → after →
    append_event). Транзакция коммитится при выходе из ``async with``; любое
    исключение откатывает ВСЮ транзакцию — ни доменной строки, ни события не
    остаётся (критерий #3, EVT-01, NFR-06).

    Публичная сигнатура стабильна — доменные сервисы Фазы 2+ строят на ней все
    одиночные операции. ``event_type``/``actor`` задаёт сервис ЯВНО (D-05); снимок
    собирается автоматически.

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
    spec = MutationSpec(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=load_row,
        schema_version=schema_version,
    )
    async with session.begin():
        return await _apply_one(session, spec)


async def mutate_many_with_event(
    session: AsyncSession,
    *,
    items: list[MutationSpec],
) -> list[Any]:
    """Применить N мутаций и записать N событий в ОДНОЙ транзакции (D-02/D-03).

    All-or-nothing: открывает единственный ``session.begin()`` и для каждого
    ``MutationSpec`` выполняет тот же per-item шаг ``_apply_one`` (один event на
    элемент, со своим before/after снимком, идентичным по форме одиночному UoW).
    Любое исключение на ЛЮБОМ элементе пробрасывается и откатывает ВЕСЬ батч —
    частичных коммитов и orphan-событий не остаётся (NFR-06). Контракт для
    bulk ``create_subtasks`` (TASK-10, план 03): N подзадач + N ``task.created``
    в одной транзакции.

    Args:
        session: свежая async-сессия (одна на батч-операцию; Anti-Pattern 5).
        items: список спецификаций мутаций; применяются по порядку в одной транзакции.

    Returns:
        Список результатов ``apply`` в порядке ``items``.
    """
    async with session.begin():
        results: list[Any] = []
        for spec in items:
            results.append(await _apply_one(session, spec))
        return results
