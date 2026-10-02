"""Read-репозиторий токенов доступа: аутентификация по токену и список для оператора.

Только ЧТЕНИЕ — событий НЕ пишет. Модуль узкий и session-injected (как ``task_read``):
транзакцию НЕ открывает. Токен в SQL не попадает вовсе — только его хэш, и только
bind-параметром.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.models import ApiToken
from novryn.domain.tokens import Principal, hash_token, looks_like_token


async def find_principal(session: AsyncSession, token: str) -> Principal | None:
    """Автор запроса по токену, либо None для неизвестного или отозванного токена.

    Заведомо неверная строка (без префикса, слишком длинная) отбрасывается без
    обращения к БД — дешёвая защита эндпоинта от мусора в заголовке.
    """
    if not looks_like_token(token):
        return None
    row = (
        await session.execute(
            select(ApiToken.id, ApiToken.holder_type, ApiToken.name).where(
                ApiToken.token_hash == hash_token(token),
                ApiToken.revoked_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Principal(actor_type=row.holder_type, actor_id=row.id, token_name=row.name)


async def list_tokens(session: AsyncSession) -> list[ApiToken]:
    """Все токены, включая отозванные, в порядке выдачи (для ``novryn token list``)."""
    result = await session.execute(
        select(ApiToken).order_by(ApiToken.created_at, ApiToken.id)
    )
    return list(result.scalars().all())
