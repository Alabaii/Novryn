"""Read-репозиторий кросс-доменных insights (INS-01/INS-02).

Ядро ценности проекта: дать Hermes максимум для реконструкции контекста. Insights
вычисляются НА ЛЕТУ SQL-агрегацией (НЕ материализуются, D-13), только ЧТЕНИЕ — событий
НЕ пишут, через UoW НЕ идут, транзакцию НЕ открывают (session-injected, как task_read).

Async-safe: только явные ``select``/скалярные агрегаты/``GROUP BY``/``FILTER``, БЕЗ lazy
relationships (иначе ``MissingGreenlet`` в async-контексте). Несколько узких агрегатных
запросов собираются в один dict в Python — НЕ один мега-JOIN (task × sessions × focus →
декартово раздувание строк, RESEARCH A6 / T-03-19).

Безопасность: ``task_id`` пересекает границу SQL ТОЛЬКО как bind-параметр Core
(``.where(... == task_id)``), не f-string (V5 / T-03-17). Возвращаются АГРЕГАТЫ
(счётчики/суммы/типы), не сырой content/evidence_json (T-03-18).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import (
    Attachment,
    DailyFocus,
    Session,
    Task,
    TaskDependency,
)


async def task_insights(session: AsyncSession, task_id: uuid.UUID) -> dict[str, object]:
    """Кросс-доменный срез по ОДНОЙ задаче на лету (INS-01, D-11).

    Несколькими узкими агрегатными запросами (RESEARCH A6, НЕ один мега-JOIN): статус
    задачи, статистика сессий (счётчики по ``result`` + суммы plan/actual), появления в
    фокусе (count + лучший rank), счётчики подзадач/активных зависимостей/активных
    вложений. Активные deps/attachments считаются с ``deleted_at IS NULL`` (как
    dependency_read). Числа нормализованы к int (coalesce к 0); срез сериализуем.

    Read-only: событий не пишет, транзакцию не открывает, lazy relationships не использует.

    Returns:
        dict с агрегатами (статус может быть ``None``, если задачи нет).
    """
    # (1) Статус задачи (скаляр; None если задачи нет).
    status = await session.scalar(
        select(Task.status).where(Task.id == task_id)
    )

    # (2) Статистика сессий: суммы + FILTER-счётчики по result в одном проходе.
    sessions_q = select(
        func.count().label("session_count"),
        func.coalesce(func.sum(Session.planned_minutes), 0).label("planned_total"),
        func.coalesce(func.sum(Session.actual_minutes), 0).label("actual_total"),
        func.count().filter(Session.result == "COMPLETED").label("completed"),
        func.count().filter(Session.result == "PARTIAL").label("partial"),
        func.count().filter(Session.result == "ABANDONED").label("abandoned"),
        func.count().filter(Session.result == "INTERRUPTED").label("interrupted"),
    ).where(Session.task_id == task_id)
    srow = (await session.execute(sessions_q)).one()

    # (3) Появления в фокусе: число снимков + лучший (минимальный) ранг.
    focus_q = select(
        func.count().label("focus_count"),
        func.min(DailyFocus.rank).label("best_rank"),
    ).where(DailyFocus.task_id == task_id)
    frow = (await session.execute(focus_q)).one()

    # (4) Подзадачи: count(Task где parent_task_id == task_id).
    subtask_count = await session.scalar(
        select(func.count()).where(Task.parent_task_id == task_id)
    )

    # (5) Активные зависимости (deleted_at IS NULL).
    dependency_count = await session.scalar(
        select(func.count()).where(
            TaskDependency.task_id == task_id,
            TaskDependency.deleted_at.is_(None),
        )
    )

    # (6) Активные вложения (deleted_at IS NULL).
    attachment_count = await session.scalar(
        select(func.count()).where(
            Attachment.task_id == task_id,
            Attachment.deleted_at.is_(None),
        )
    )

    return {
        "task_id": str(task_id),
        "status": status,
        "sessions": {
            "count": int(srow.session_count),
            "planned_minutes_total": int(srow.planned_total),
            "actual_minutes_total": int(srow.actual_total),
            "completed": int(srow.completed),
            "partial": int(srow.partial),
            "abandoned": int(srow.abandoned),
            "interrupted": int(srow.interrupted),
        },
        "focus": {
            "appearances": int(frow.focus_count),
            "best_rank": int(frow.best_rank) if frow.best_rank is not None else None,
        },
        "subtask_count": int(subtask_count or 0),
        "active_dependency_count": int(dependency_count or 0),
        "active_attachment_count": int(attachment_count or 0),
    }
