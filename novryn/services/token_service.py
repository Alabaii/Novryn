"""Сервис токенов доступа: выдача и отзыв (Фаза 4, PERM-04).

Выдача и отзыв — значимые изменения безопасности, поэтому идут через UoW и пишут
``token.issued``/``token.revoked`` в журнал. Снимок токена в событии собирается БЕЗ
``token_hash``: журнал append-only, и вычистить оттуда производную секрета потом
нельзя. Сам токен существует только в ответе ``issue_token`` — в БД лежит его хэш.

Обычно сервис вызывает CLI оператора (``novryn token ...``) от имени SYSTEM, но автор
события, как и во всех сервисах, задаётся вызывающим явно.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import RowMapping, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import ApiToken
from novryn.domain.events import EventType
from novryn.domain.tokens import generate_token, hash_token
from novryn.repositories.uow import mutate_with_event

# Все колонки токена, кроме хэша, — форма снимка для журнала. Новые колонки
# попадут в снимок автоматически, хэш не попадёт никогда.
_SNAPSHOT_COLUMNS = [c for c in ApiToken.__table__.columns if c.key != "token_hash"]


@dataclass(frozen=True)
class IssuedToken:
    """Результат выдачи: ``token`` показывается один раз и больше нигде не хранится."""

    id: uuid.UUID
    token: str


async def _load_snapshot(session: AsyncSession, token_id: uuid.UUID) -> RowMapping | None:
    """Строка токена для снимков before/after — без ``token_hash``."""
    result = await session.execute(
        select(*_SNAPSHOT_COLUMNS).where(ApiToken.id == token_id)
    )
    return result.mappings().one_or_none()


async def issue_token(
    session: AsyncSession,
    *,
    name: str,
    holder_type: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> IssuedToken:
    """Выдать токен держателю ``holder_type`` (USER/HERMES) + событие token.issued.

    Держатель вне USER/HERMES отвергается CHECK ``ck_api_tokens_holder_type`` на
    write (``IntegrityError``) — Python-проверки нет, как и в остальных сервисах.

    Args:
        session: свежая async-сессия (одна на операцию; Anti-Pattern 5).
        name: человекочитаемое имя токена, например ``hermes@orangepi``.
        holder_type: кого токен аутентифицирует — тип актора для его событий.
        actor_type: кто выдаёт токен (обычно SYSTEM — оператор через CLI).
        actor_id: id выдающего или None.

    Returns:
        id токена и сам токен (единственный раз, когда он доступен).
    """
    token_id = new_id()
    token = generate_token()

    async def apply(s: AsyncSession) -> None:
        await s.execute(
            insert(ApiToken).values(
                id=token_id,
                name=name,
                holder_type=holder_type,
                token_hash=hash_token(token),
            )
        )

    await mutate_with_event(
        session,
        entity_type="api_token",
        entity_id=token_id,
        event_type=EventType.TOKEN_ISSUED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: _load_snapshot(s, token_id),
    )
    return IssuedToken(id=token_id, token=token)


async def revoke_token(
    session: AsyncSession,
    *,
    token_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Отозвать токен + token.revoked; no-op без события, если его нет или он уже отозван.

    Идемпотентность — как у ``detach_resource`` (D-08): повторный отзыв не плодит
    события. Строка не удаляется: отозванный токен остаётся в истории.

    Returns:
        id отозванного токена, либо None при no-op.
    """
    active = (
        await session.execute(
            select(ApiToken.id).where(
                ApiToken.id == token_id, ApiToken.revoked_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    await session.rollback()  # закрыть read-autobegin перед явным begin() UoW
    if active is None:
        return None

    async def apply(s: AsyncSession) -> None:
        await s.execute(
            update(ApiToken)
            .where(ApiToken.id == token_id)
            .values(revoked_at=func.now())
        )

    await mutate_with_event(
        session,
        entity_type="api_token",
        entity_id=token_id,
        event_type=EventType.TOKEN_REVOKED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: _load_snapshot(s, token_id),
    )
    return token_id
