"""Справочник `event_type` и `actor_type` для Event Store (D-05, A4).

Назначение: единый источник истины по бизнес-смысловым именам событий и типам
акторов. Доменные сервисы Фазы 2+ передают эти константы в UoW ЯВНО — `event_type`
НЕ выводится из dirty-трекинга SQLAlchemy (D-05): обезличенные created/updated/
deleted из listener'а запрещены, потому что не несут бизнес-смысла
(`task.completed` vs `task.archived`).

Нотация `event_type` — точечная `<entity>.<verb>` (согласовано с
01-RESEARCH.md / ARCHITECTURE.md). Прошедшее время глагола = факт уже свершился
(Event Store фиксирует факты, не команды).

Коррекции (D-07) выражаются КОМПЕНСИРУЮЩИМ событием с суффиксом `.invalidated`
(см. ``invalidated`` ниже), а НЕ правкой/удалением строки события — таблица
events append-only (NFR-06/EVT-03).

PII-замечание (T-01-16): при логировании события писать ТОЛЬКО `event_type` и
`entity_id`, никогда полный `payload_json` — он может содержать `ai_context_json`
с персональными данными.
"""

from typing import Final


class EventType:
    """Канонические имена событий (`<entity>.<verb>`, прошедшее время).

    Справочник для доменных сервисов Фазы 2+. Передаётся в UoW явно (D-05).
    Список покрывает все 8 сущностей PRD §4–§11; расширяется по мере роста
    доменной логики в следующих фазах.
    """

    # tasks (PRD §4) — жизненный цикл задачи.
    TASK_CREATED: Final = "task.created"
    TASK_UPDATED: Final = "task.updated"
    TASK_COMPLETED: Final = "task.completed"
    TASK_ARCHIVED: Final = "task.archived"
    TASK_BLOCKED: Final = "task.blocked"
    TASK_UNBLOCKED: Final = "task.unblocked"

    # sessions (PRD §7) — попытка выполнения.
    SESSION_STARTED: Final = "session.started"
    SESSION_ENDED: Final = "session.ended"

    # task_dependencies (PRD §5).
    DEPENDENCY_LINKED: Final = "dependency.linked"
    DEPENDENCY_UNLINKED: Final = "dependency.unlinked"

    # attachments (PRD §6).
    ATTACHMENT_ATTACHED: Final = "attachment.attached"
    ATTACHMENT_DETACHED: Final = "attachment.detached"

    # daily_focus (PRD §9) — снимок решения Hermes на день.
    FOCUS_GENERATED: Final = "focus.generated"

    # user_memory (PRD §10) / behavior_patterns (PRD §11).
    # Upsert по memory_type/pattern_type (D-05): первая запись → `.stored`, повторная
    # запись того же типа обновляет строку in-place и пишет `.updated`. История
    # эволюции confidence/content полностью восстанавливается из Event Store
    # (последовательность .stored → .updated → ...), хотя строка одна.
    MEMORY_STORED: Final = "memory.stored"
    MEMORY_UPDATED: Final = "memory.updated"
    PATTERN_STORED: Final = "pattern.stored"
    PATTERN_UPDATED: Final = "pattern.updated"

    # Компенсирующее событие (D-07): отменяет ранее зафиксированный факт.
    # Доменный сервис формирует конкретное имя как f"{base}.invalidated"
    # (например "task.completed.invalidated"); INVALIDATED — суффикс-якорь.
    INVALIDATED: Final = "invalidated"


class ActorType:
    """Тип актора, инициировавшего изменение (A4; events.actor_type CHECK).

    Множество ограничено CHECK ``ck_events_actor_type`` на уровне БД
    (novryn/db/models.py). В Фазе 1 actor передаётся сервисом; подлинность из
    токена — Фаза 4 (PERM-04, T-01-17).
    """

    USER: Final = "USER"
    HERMES: Final = "HERMES"
    SYSTEM: Final = "SYSTEM"  # reconciliation / seed-события без actor_id
