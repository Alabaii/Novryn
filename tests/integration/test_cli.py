"""Интеграция (Фаза 4, план 02): CLI ``novryn token create|list|revoke``.

Wave-0 RED baseline: импортирует ещё НЕ существующий novryn.cli (collection-error до 04-02).

Поведение, фиксируемое тестами:
- create печатает в stdout ТОЛЬКО токен (удобно для скриптов), предупреждение — в stderr;
- list показывает имена и статус, но не токены и не их хэши;
- revoke отключает токен; повторный отзыв — код возврата 1;
- держатель вне USER/HERMES отвергается разбором аргументов.

Синхронные тесты: CLI сам поднимает ``asyncio.run`` — из работающего event loop его не
вызвать. ``DATABASE_URL`` выставляет фикстура ``database_url`` (через ``migrated_db``).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from novryn.cli import main
from novryn.domain.events import ActorType
from novryn.domain.tokens import TOKEN_PREFIX, Principal, hash_token
from novryn.repositories.token_read import find_principal


async def _principal(database_url: str, token: str) -> Principal | None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            return await find_principal(s, token)
    finally:
        await engine.dispose()


def test_token_lifecycle(migrated_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["token", "create", "--holder", "hermes", "--name", "hermes@cli"]) == 0
    created = capsys.readouterr()
    token = created.out.strip()
    assert token.startswith(TOKEN_PREFIX)
    assert "один раз" in created.err

    principal = asyncio.run(_principal(migrated_db, token))
    assert principal is not None
    assert principal.actor_type == ActorType.HERMES
    assert principal.token_name == "hermes@cli"

    assert main(["token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "hermes@cli" in listing
    assert str(principal.actor_id) in listing
    assert token not in listing
    assert hash_token(token) not in listing

    assert main(["token", "revoke", str(principal.actor_id)]) == 0
    capsys.readouterr()
    assert asyncio.run(_principal(migrated_db, token)) is None

    assert main(["token", "revoke", str(principal.actor_id)]) == 1
    assert "не найден или уже отозван" in capsys.readouterr().err


def test_system_holder_rejected_by_cli(migrated_db: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["token", "create", "--holder", "system", "--name", "x"])
    assert exc.value.code == 2
