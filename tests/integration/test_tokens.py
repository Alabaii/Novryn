"""Интеграция (Фаза 4, план 02 / PERM-04): токены доступа к MCP-эндпоинту.

Wave-0 RED baseline: импортирует ещё НЕ существующие novryn.domain.tokens,
novryn.services.token_service и novryn.repositories.token_read (collection-error до 04-02).

Поведение, фиксируемое тестами:
- issue_token возвращает токен с префиксом ровно один раз; в БД — только SHA-256 хэш;
- token.issued/token.revoked пишутся в журнал БЕЗ токена и без его хэша
  (журнал append-only — вычистить секрет оттуда потом нельзя);
- find_principal по активному токену даёт автора для событий: actor_type держателя,
  actor_id = id токена;
- неизвестный, отозванный или заведомо неверный токен → None; заведомо неверный
  отбрасывается без обращения к БД;
- revoke_token идемпотентен: повторный отзыв — no-op без события;
- держатель SYSTEM запрещён (CHECK в БД).

Свежая AsyncSession на операцию (Pitfall 3); зависит от migrated_db. Реальный PostgreSQL.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import ApiToken, Event
from novryn.domain.events import ActorType, EventType
from novryn.domain.tokens import TOKEN_PREFIX, Principal, generate_token, hash_token
from novryn.repositories.token_read import find_principal, list_tokens
from novryn.services.token_service import IssuedToken, issue_token, revoke_token
from tests.integration.conftest import count_queries


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _issue(
    maker: async_sessionmaker[AsyncSession],
    name: str,
    holder_type: str = ActorType.HERMES,
) -> IssuedToken:
    async with maker() as s:
        return await issue_token(
            s,
            name=name,
            holder_type=holder_type,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )


async def _events(check: AsyncSession, token_id: uuid.UUID) -> list[Event]:
    rows = await check.execute(
        select(Event).where(Event.entity_id == token_id).order_by(Event.occurred_at)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_issue_returns_token_once_and_stores_only_hash(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    issued = await _issue(uow_sessionmaker, "hermes@test")
    assert issued.token.startswith(TOKEN_PREFIX)
    async with uow_sessionmaker() as check:
        row = await check.get(ApiToken, issued.id)
        assert row is not None
        assert row.token_hash == hash_token(issued.token)
        assert row.holder_type == ActorType.HERMES
        assert row.revoked_at is None


@pytest.mark.asyncio
async def test_journal_has_no_token_secrets(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    issued = await _issue(uow_sessionmaker, "secret-check")
    async with uow_sessionmaker() as s:
        await revoke_token(
            s, token_id=issued.id, actor_type=ActorType.SYSTEM, actor_id=None
        )
    async with uow_sessionmaker() as check:
        events = await _events(check, issued.id)
    assert [e.event_type for e in events] == [
        EventType.TOKEN_ISSUED,
        EventType.TOKEN_REVOKED,
    ]
    assert all(e.entity_type == "api_token" for e in events)
    assert all(e.actor_type == ActorType.SYSTEM for e in events)
    dumped = json.dumps([e.payload_json for e in events])
    assert issued.token not in dumped
    assert hash_token(issued.token) not in dumped
    assert "token_hash" not in dumped
    issued_after = events[0].payload_json["after"]
    assert isinstance(issued_after, dict)
    assert issued_after["name"] == "secret-check"


@pytest.mark.asyncio
async def test_active_token_resolves_to_principal(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    issued = await _issue(uow_sessionmaker, "user@laptop", holder_type=ActorType.USER)
    async with uow_sessionmaker() as check:
        principal = await find_principal(check, issued.token)
    assert principal == Principal(
        actor_type=ActorType.USER, actor_id=issued.id, token_name="user@laptop"
    )


@pytest.mark.asyncio
async def test_unknown_token_has_no_principal(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _issue(uow_sessionmaker, "real")
    never_issued = generate_token()
    async with uow_sessionmaker() as check:
        assert await find_principal(check, never_issued) is None


@pytest.mark.asyncio
async def test_malformed_token_rejected_without_query(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
) -> None:
    async with uow_sessionmaker() as check:
        with count_queries(async_engine) as counter:
            assert await find_principal(check, "") is None
            assert await find_principal(check, "Bearer something") is None
            assert await find_principal(check, TOKEN_PREFIX + "x" * 1000) is None
    assert counter["n"] == 0


@pytest.mark.asyncio
async def test_revoke_disables_token_and_is_idempotent(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    issued = await _issue(uow_sessionmaker, "to-revoke")
    async with uow_sessionmaker() as s:
        assert (
            await revoke_token(
                s, token_id=issued.id, actor_type=ActorType.SYSTEM, actor_id=None
            )
            == issued.id
        )
    async with uow_sessionmaker() as check:
        assert await find_principal(check, issued.token) is None
        row = await check.get(ApiToken, issued.id)
        assert row is not None
        assert row.revoked_at is not None

    async with uow_sessionmaker() as s:
        assert (
            await revoke_token(
                s, token_id=issued.id, actor_type=ActorType.SYSTEM, actor_id=None
            )
            is None
        )
    async with uow_sessionmaker() as check:
        revoked = [
            e for e in await _events(check, issued.id)
            if e.event_type == EventType.TOKEN_REVOKED
        ]
    assert len(revoked) == 1


@pytest.mark.asyncio
async def test_revoke_unknown_token_is_noop(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    unknown = new_id()
    async with uow_sessionmaker() as s:
        assert (
            await revoke_token(
                s, token_id=unknown, actor_type=ActorType.SYSTEM, actor_id=None
            )
            is None
        )
    async with uow_sessionmaker() as check:
        assert await _events(check, unknown) == []


@pytest.mark.asyncio
async def test_system_holder_rejected(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IntegrityError):
        await _issue(uow_sessionmaker, "system", holder_type=ActorType.SYSTEM)


@pytest.mark.asyncio
async def test_list_tokens_includes_revoked(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    active = await _issue(uow_sessionmaker, "list-active")
    revoked = await _issue(uow_sessionmaker, "list-revoked")
    async with uow_sessionmaker() as s:
        await revoke_token(
            s, token_id=revoked.id, actor_type=ActorType.SYSTEM, actor_id=None
        )
    async with uow_sessionmaker() as check:
        by_id = {t.id: t for t in await list_tokens(check)}
    assert by_id[active.id].revoked_at is None
    assert by_id[revoked.id].revoked_at is not None
