"""ORM-модели схемы Novryn (SQLAlchemy 2.0 declarative).

Источник истины по форме данных: типы колонок, NULL-политика, FK, именованные
enum-CHECK и обязательные поля события. Модели — основа для рукописной миграции
001 (план 03) и всех будущих репозиториев.

Конвенции (CLAUDE.md / 01-RESEARCH.md):
- Все id — UUID v7 app-level через ``default=new_id`` (случайные UUID версии 4
  запрещены, серверный gen_random_uuid не используется). NFR-01.
- Все timestamp-колонки — ``TIMESTAMPTZ`` (``DateTime(timezone=True)``);
  created_at/occurred_at имеют ``server_default=func.now()``.
- Enum-поля — ``VARCHAR`` + именованный ``CHECK`` с каноническими значениями из
  PRD/REQUIREMENTS (верхний регистр); native PG ENUM не используется (D-08).
- Имена CHECK-constraint стабильны (нужны для проверки в плане 03).
- relationship с авто-lazy в async не объявляются (MissingGreenlet); для Фазы 1
  достаточно FK-колонок.
"""

import datetime
import decimal
import uuid

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novryn.db.base import Base
from novryn.db.ids import new_id

# Канонические enum-множества (PRD §4–§7; REQUIREMENTS). Верхний регистр (RESEARCH A1).
_TASK_STATUS = ("INBOX", "TODO", "IN_PROGRESS", "BLOCKED", "DONE", "ARCHIVED")
_TASK_ENERGY = ("LOW", "MEDIUM", "HIGH")
_ATTACHMENT_TYPE = ("URL", "DOCUMENT", "PDF", "GITHUB", "GOOGLE_DOC", "OTHER")
_SESSION_RESULT = ("COMPLETED", "PARTIAL", "ABANDONED", "INTERRUPTED")


def _in_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """Построить именованный CHECK ``column IN (...)`` из канонических значений."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class Task(Base):
    """Задача — основная сущность системы (PRD §4)."""

    __tablename__ = "tasks"
    __table_args__ = (
        _in_check("status", _TASK_STATUS, "ck_tasks_status"),
        _in_check("energy_required", _TASK_ENERGY, "ck_tasks_energy"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="INBOX")
    due_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=True
    )
    user_time_estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    ai_time_estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    energy_required: Mapped[str | None] = mapped_column(String(10))
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    ai_context_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class TaskDependency(Base):
    """Зависимость задачи от другой задачи (PRD §5)."""

    __tablename__ = "task_dependencies"
    __table_args__ = (
        CheckConstraint(
            "task_id <> depends_on_task_id", name="ck_task_dep_no_self"
        ),  # DEP-04: задача не зависит от себя; циклы — Фаза 2 (сервис).
        UniqueConstraint(
            "task_id", "depends_on_task_id", name="uq_task_dependencies_pair"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=False
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attachment(Base):
    """Внешний ресурс, связанный с задачей (PRD §6)."""

    __tablename__ = "attachments"
    __table_args__ = (
        _in_check("type", _ATTACHMENT_TYPE, "ck_attachments_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Session(Base):
    """Сессия — попытка выполнения задачи (PRD §7)."""

    __tablename__ = "sessions"
    __table_args__ = (
        _in_check("result", _SESSION_RESULT, "ck_sessions_result"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=False
    )
    planned_minutes: Mapped[int | None] = mapped_column(Integer)
    actual_minutes: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)


class DailyFocus(Base):
    """Снимок решения Hermes на конкретный день (PRD §9)."""

    __tablename__ = "daily_focus"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(Text)


class UserMemory(Base):
    """Долгосрочная память о пользователе (PRD §10)."""

    __tablename__ = "user_memory"
    __table_args__ = (
        CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0", name="ck_user_memory_confidence"
        ),  # MEM-02
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    memory_type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[decimal.Decimal | None] = mapped_column(Numeric(3, 2))
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BehaviorPattern(Base):
    """Формализованное наблюдение Hermes о поведении (PRD §11)."""

    __tablename__ = "behavior_patterns"
    __table_args__ = (
        CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0", name="ck_behavior_confidence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    pattern_type: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[decimal.Decimal | None] = mapped_column(Numeric(3, 2))
    evidence_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
