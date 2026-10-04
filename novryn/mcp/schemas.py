"""Схемы ответов инструментов: из них SDK строит outputSchema и структурный ответ.

Модели читают ORM-строки напрямую (``from_attributes``) и публикуют только то, что
нужно потребителю: служебные колонки (``search_vector``, ``deleted_at``, хэш токена)
наружу не выходят. ``extra="forbid"`` ловит расхождение со словарями, которые отдают
сервисы (insights): лишний или пропавший ключ — ошибка в тестах, а не тихо
устаревшая схема. Время — UTC, ISO 8601; confidence — число 0.0–1.0.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from novryn.domain.enums import AttachmentType, SessionResult, TaskEnergy, TaskStatus

ItemT = TypeVar("ItemT")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Page(_Out, Generic[ItemT]):
    """Страница выдачи. Следующая — тот же запрос с ``offset = next_offset``."""

    items: list[ItemT]
    offset: int
    has_more: bool = Field(description="Есть ли записи после этой страницы.")
    next_offset: int | None = Field(description="offset следующей страницы или null.")

    @classmethod
    def of(
        cls,
        rows: Sequence[object],
        *,
        limit: int,
        offset: int,
        convert: Callable[[Any], ItemT],
    ) -> Self:
        """Собрать страницу из выборки ``limit + 1``: лишняя строка лишь сообщает has_more."""
        has_more = len(rows) > limit
        return cls(
            items=[convert(row) for row in rows[:limit]],
            offset=offset,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )


class TaskOut(_Out):
    """Полная карточка задачи."""

    id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    due_date: datetime.datetime | None
    parent_task_id: uuid.UUID | None = Field(description="Родительская задача или null.")
    user_time_estimate_minutes: int | None = Field(description="Оценка пользователя.")
    ai_time_estimate_minutes: int | None = Field(description="Оценка Hermes.")
    energy_required: TaskEnergy | None
    blocked_reason: str | None
    ai_context_json: dict[str, Any] | None = Field(
        description="Свободный контекст Hermes: его интерпретация задачи."
    )
    created_at: datetime.datetime
    updated_at: datetime.datetime | None
    completed_at: datetime.datetime | None
    archived_at: datetime.datetime | None


class TaskTreeNode(_Out):
    """Узел дерева задач: задача и её подзадачи."""

    id: uuid.UUID
    parent_task_id: uuid.UUID | None
    title: str
    status: TaskStatus
    children: list[TaskTreeNode]


class DependencyOut(_Out):
    """Связь «task_id зависит от depends_on_task_id»: сначала нужно сделать вторую."""

    id: uuid.UUID
    task_id: uuid.UUID
    depends_on_task_id: uuid.UUID
    created_at: datetime.datetime


class TaskDependencies(_Out):
    task_id: uuid.UUID
    dependencies: list[DependencyOut]


class AttachmentOut(_Out):
    """Внешний ресурс задачи: ссылка, документ, репозиторий."""

    id: uuid.UUID
    task_id: uuid.UUID
    type: AttachmentType
    title: str | None
    url: str | None
    metadata_json: dict[str, Any] | None
    created_at: datetime.datetime


class TaskResources(_Out):
    task_id: uuid.UUID
    resources: list[AttachmentOut]


class SessionOut(_Out):
    """Попытка поработать над задачей: план, факт, итог."""

    id: uuid.UUID
    task_id: uuid.UUID
    planned_minutes: int | None
    actual_minutes: int | None
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None = Field(description="null — сессия ещё идёт.")
    result: SessionResult | None
    notes: str | None


class TaskSessions(_Out):
    task_id: uuid.UUID
    sessions: list[SessionOut]


class FocusItem(_Out):
    """Пункт фокуса дня: задача, её место в списке и причина выбора."""

    rank: int = Field(description="1 — самое важное.")
    task_id: uuid.UUID
    title: str
    status: TaskStatus
    reason: str | None = Field(description="Почему Hermes выбрал задачу.")


class Focus(_Out):
    """Фокус дня — последняя версия снимка на дату."""

    date: datetime.date
    focus_set_id: uuid.UUID = Field(description="id версии снимка.")
    generated_at: datetime.datetime
    generated_by: str | None
    items: list[FocusItem]


class FocusNow(_Out):
    """Главная задача момента — первый пункт фокуса дня."""

    date: datetime.date
    focus_set_id: uuid.UUID
    item: FocusItem


class MemoryOut(_Out):
    """Долгосрочное знание о пользователе — одна запись на memory_type."""

    id: uuid.UUID
    memory_type: str
    content: str | None
    confidence: float | None
    source: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime | None


class PatternOut(_Out):
    """Поведенческий паттерн — наблюдение Hermes с доказательствами."""

    id: uuid.UUID
    pattern_type: str
    confidence: float | None
    evidence_json: dict[str, Any] | None
    created_at: datetime.datetime
    updated_at: datetime.datetime | None


class Patterns(_Out):
    patterns: list[PatternOut]


class SessionStats(_Out):
    count: int
    planned_minutes_total: int
    actual_minutes_total: int
    completed: int
    partial: int
    abandoned: int
    interrupted: int


class FocusStats(_Out):
    appearances: int = Field(description="Сколько раз задача попадала в фокус.")
    best_rank: int | None


class TaskInsights(_Out):
    """Сводка по задаче: сессии, фокус, подзадачи и активные связи."""

    task_id: uuid.UUID
    status: TaskStatus
    sessions: SessionStats
    focus: FocusStats
    subtask_count: int
    active_dependency_count: int
    active_attachment_count: int


class TaskStats(_Out):
    total: int
    by_status: dict[str, int]
    completed: int
    completion_ratio: float | None = Field(description="Доля DONE среди всех задач.")


class TimeStats(_Out):
    tracked_minutes_total: int
    avg_session_minutes: float | None


class PatternBrief(_Out):
    pattern_type: str
    confidence: float | None


class MemoryStats(_Out):
    total: int
    by_type: dict[str, int]


class FocusActivity(_Out):
    active_days: int
    last_date: datetime.date | None


class UserInsights(_Out):
    """Сводка по пользователю: задачи, время, ведущие паттерны, память, фокус."""

    tasks: TaskStats
    time: TimeStats
    top_patterns: list[PatternBrief]
    memory: MemoryStats
    focus: FocusActivity


class EventOut(_Out):
    """Событие журнала: что произошло, с какой сущностью, кто сделал, снимки до/после."""

    id: uuid.UUID
    occurred_at: datetime.datetime
    event_type: str
    entity_type: str
    entity_id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    schema_version: int
    payload_json: dict[str, Any] = Field(description="Снимки {before, after}.")
