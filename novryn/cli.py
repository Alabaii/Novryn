"""Командная строка оператора: ``novryn token create|list|revoke``.

Запускается на сервере человеком с прямым доступом к БД (``DATABASE_URL``), поэтому
события выдачи и отзыва пишутся от имени SYSTEM. Команда ``serve`` появится в 04-05.

Данные печатаются в stdout (токен при create, таблица при list), сообщения человеку —
в stderr. Так ``TOKEN=$(novryn token create ...)`` получает ровно токен.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from novryn.config import get_database_url
from novryn.domain.events import ActorType
from novryn.repositories.token_read import list_tokens
from novryn.services.token_service import issue_token, revoke_token

_HOLDERS = {"user": ActorType.USER, "hermes": ActorType.HERMES}

Handler = Callable[[AsyncSession, argparse.Namespace], Awaitable[int]]


async def _create(session: AsyncSession, args: argparse.Namespace) -> int:
    issued = await issue_token(
        session,
        name=args.name,
        holder_type=_HOLDERS[args.holder],
        actor_type=ActorType.SYSTEM,
        actor_id=None,
    )
    print(issued.token)
    print(
        f"Токен «{args.name}» ({args.holder}, id {issued.id}) выдан. "
        "Он показан один раз — сохраните его сейчас.",
        file=sys.stderr,
    )
    return 0


def _local_time(moment: datetime.datetime | None) -> str:
    return "—" if moment is None else moment.astimezone().strftime("%Y-%m-%d %H:%M")


async def _list(session: AsyncSession, args: argparse.Namespace) -> int:
    tokens = await list_tokens(session)
    if not tokens:
        print("Токенов нет.", file=sys.stderr)
        return 0
    print(f"{'ID':<36}  {'ДЕРЖАТЕЛЬ':<9}  {'ВЫДАН':<16}  {'ОТОЗВАН':<16}  ИМЯ")
    for t in tokens:
        print(
            f"{t.id!s:<36}  {t.holder_type:<9}  {_local_time(t.created_at):<16}  "
            f"{_local_time(t.revoked_at):<16}  {t.name}"
        )
    return 0


async def _revoke(session: AsyncSession, args: argparse.Namespace) -> int:
    revoked = await revoke_token(
        session, token_id=args.token_id, actor_type=ActorType.SYSTEM, actor_id=None
    )
    if revoked is None:
        print(f"Токен {args.token_id} не найден или уже отозван.", file=sys.stderr)
        return 1
    print(f"Токен {revoked} отозван.", file=sys.stderr)
    return 0


_TOKEN_HANDLERS: dict[str, Handler] = {
    "create": _create,
    "list": _list,
    "revoke": _revoke,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novryn", description="Novryn — источник истины.")
    commands = parser.add_subparsers(dest="command", required=True)

    token = commands.add_parser("token", help="токены доступа к MCP-эндпоинту")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    create = token_commands.add_parser("create", help="выдать токен (печатается один раз)")
    create.add_argument(
        "--holder", required=True, choices=sorted(_HOLDERS), help="кого аутентифицирует"
    )
    create.add_argument("--name", required=True, help="имя, например hermes@orangepi")
    token_commands.add_parser("list", help="показать токены (без секретов)")
    revoke = token_commands.add_parser("revoke", help="отозвать токен")
    revoke.add_argument("token_id", type=uuid.UUID, help="id токена из «token list»")
    return parser


async def _run(database_url: str, handler: Handler, args: argparse.Namespace) -> int:
    # Свой короткоживущий движок на вызов: CLI живёт один asyncio.run, пул не нужен.
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await handler(session, args)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа ``novryn``; возвращает код выхода."""
    args = _parser().parse_args(argv)
    try:
        database_url = get_database_url()
    except (RuntimeError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(database_url, _TOKEN_HANDLERS[args.token_command], args))
    except BrokenPipeError:
        # Читатель stdout закрыл канал раньше (например, «| head») — это не ошибка
        # команды. Уводим stdout в /dev/null, чтобы Python не упал на финальном
        # сбросе буфера (рецепт из документации модуля signal, раздел SIGPIPE).
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
