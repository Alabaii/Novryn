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

import decimal
import uuid

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import (
    Attachment,
    BehaviorPattern,
    DailyFocus,
    Session,
    Task,
    TaskDependency,
    UserMemory,
)

# Размер выдачи топа паттернов в user_insights (D-12) — небольшой, без DoS-разбега.
_TOP_PATTERNS_LIMIT = 5


def _to_float(value: object) -> float | None:
    """Нормализовать Decimal/None из AVG к сериализуемому float (или None)."""
    if value is None:
        return None
    if isinstance(value, decimal.Decimal):
        return float(value)
    return float(value)  # type: ignore[arg-type]


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


async def user_insights(session: AsyncSession) -> dict[str, object]:
    """Полный кросс-доменный срез пользователя на лету (INS-02, D-12).

    Несколькими узкими агрегатными запросами (RESEARCH A6, НЕ один мега-JOIN): задачи по
    статусам (GROUP BY), суммарное/среднее отслеженное время, доля завершённых (DONE/всего),
    топ behavior_patterns по confidence, сводка user_memory (count + GROUP BY memory_type),
    активность фокуса (число дат + последняя дата). Decimal/None нормализованы к
    сериализуемым числам.

    Read-only: событий не пишет, транзакцию не открывает, lazy relationships не использует.

    Returns:
        dict с полным срезом (поля могут быть ``0``/``None`` при пустой БД).
    """
    # (1) Задачи по статусам: GROUP BY status.
    status_rows = (
        await session.execute(
            select(Task.status, func.count()).group_by(Task.status)
        )
    ).all()
    tasks_by_status = {status: int(cnt) for status, cnt in status_rows}
    total_tasks = sum(tasks_by_status.values())
    done_tasks = tasks_by_status.get("DONE", 0)
    # (4) Доля завершённых задач: DONE / всего (из агрегата статусов).
    completion_ratio = (done_tasks / total_tasks) if total_tasks else None

    # (2)(3) Суммарное и среднее отслеженное время по всем сессиям.
    time_q = select(
        func.coalesce(func.sum(Session.actual_minutes), 0).label("tracked_total"),
        func.avg(Session.actual_minutes).label("avg_session"),
    )
    trow = (await session.execute(time_q)).one()

    # (5) Топ behavior_patterns по confidence (агрегаты — только тип/уверенность, не evidence).
    pattern_rows = (
        await session.execute(
            select(BehaviorPattern.pattern_type, BehaviorPattern.confidence)
            # Вторичный ключ created_at DESC (WR-04): без него при равном
            # confidence срез top-5 недетерминирован между вызовами. Согласовано
            # с tie-break соседних выборок (pattern_read/memory_read).
            .order_by(desc(BehaviorPattern.confidence), desc(BehaviorPattern.created_at))
            .limit(_TOP_PATTERNS_LIMIT)
        )
    ).all()
    top_patterns = [
        {"pattern_type": pt, "confidence": _to_float(conf)}
        for pt, conf in pattern_rows
    ]

    # (6) Сводка user_memory: общий count + распределение по memory_type.
    memory_total = await session.scalar(select(func.count()).select_from(UserMemory))
    memory_type_rows = (
        await session.execute(
            select(UserMemory.memory_type, func.count()).group_by(UserMemory.memory_type)
        )
    ).all()
    memory_by_type = {mt: int(cnt) for mt, cnt in memory_type_rows}

    # (7) Активность фокуса: число уникальных дат + последняя дата.
    focus_q = select(
        func.count(func.distinct(DailyFocus.date)).label("focus_days"),
        func.max(DailyFocus.date).label("last_focus_date"),
    )
    focus_row = (await session.execute(focus_q)).one()

    return {
        "tasks": {
            "total": total_tasks,
            "by_status": tasks_by_status,
            "completed": done_tasks,
            "completion_ratio": completion_ratio,
        },
        "time": {
            "tracked_minutes_total": int(trow.tracked_total),
            "avg_session_minutes": _to_float(trow.avg_session),
        },
        "top_patterns": top_patterns,
        "memory": {
            "total": int(memory_total or 0),
            "by_type": memory_by_type,
        },
        "focus": {
            "active_days": int(focus_row.focus_days),
            "last_date": (
                focus_row.last_focus_date.isoformat()
                if focus_row.last_focus_date is not None
                else None
            ),
        },
    }
