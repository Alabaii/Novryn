"""Сервис сессий выполнения (PRD §7; SESS-01..04).

``start_session`` фиксирует факт начала попытки (session.started); ``finish_session`` —
факт завершения (session.ended). Решения семантики (RESEARCH A4–A7, делегированы
пользователем — минимальные «хранилище фактов» по умолчанию):
- A4: start НЕ требует и НЕ меняет статус задачи (статусом управляет Hermes отдельно);
- A5: несколько одновременно открытых сессий на задачу разрешены (нет guard'а);
- A6: finish требует actual_minutes, ended_at(=now), result(enum), notes опциональны;
- A7: get_sessions — по started_at, id ASC (см. dependency_read.get_sessions).

finish_session guard'ит существование (NotFoundError) — завершать несуществующую
сессию = фантомное событие; запрещаем (целостность аудита).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import Session
from novryn.domain.errors import NotFoundError
from novryn.domain.events import EventType
from novryn.repositories.uow import mutate_with_event


async def start_session(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    planned_minutes: int,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID:
    """Начать сессию: planned_minutes + started_at=now + session.started (SESS-01).

    НЕ меняет статус задачи (A4); нет ограничения «одна активная сессия» (A5).

    Returns:
        id созданной сессии.
    """
    sess_id = new_id()

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            insert(Session).values(
                id=sess_id,
                task_id=task_id,
                planned_minutes=planned_minutes,
                started_at=func.now(),
            )
        )
        return sess_id

    await mutate_with_event(
        session,
        entity_type="session",
        entity_id=sess_id,
        event_type=EventType.SESSION_STARTED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Session, sess_id),
    )
    return sess_id


async def finish_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    actual_minutes: int,
    result: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    notes: str | None = None,
) -> uuid.UUID:
    """Завершить сессию: actual_minutes + ended_at=now + result + notes + session.ended.

    ``result`` ограничен enum ck_sessions_result (SESS-03); notes опциональны (A6).

    Returns:
        id завершённой сессии.

    Raises:
        NotFoundError: если сессии с таким id нет.
    """
    if await session.get(Session, session_id) is None:
        raise NotFoundError(session_id, entity_type="session")
    await session.rollback()  # закрыть read-autobegin перед явным begin() UoW

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(
                actual_minutes=actual_minutes,
                ended_at=func.now(),
                result=result,
                notes=notes,
            )
        )
        return session_id

    await mutate_with_event(
        session,
        entity_type="session",
        entity_id=session_id,
        event_type=EventType.SESSION_ENDED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Session, session_id),
    )
    return session_id
