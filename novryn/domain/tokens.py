"""Токены доступа к MCP-эндпоинту: формат, хэш и аутентифицированный автор (PERM-04).

Чистый доменный слой: только stdlib, ничего из SQLAlchemy. Хранилище и сервисы
(``token_service``/``token_read``) опираются на эти функции, MCP-слой Фазы 4 — на
``Principal``.

Формат токена: ``nvr_`` + 256 бит случайности (base64url). Префикс отличает токен
Novryn от чужих строк (сканеры секретов, мусор в заголовке) и позволяет отбросить
заведомо неверное значение без обращения к БД.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

TOKEN_PREFIX: Final = "nvr_"
# 32 случайных байта = 256 бит энтропии → 43 символа base64url после префикса.
_TOKEN_RANDOM_BYTES: Final = 32
# Строка длиннее этого заведомо не наш токен — до хэширования и запроса не доходит.
_MAX_TOKEN_LENGTH: Final = 128


@dataclass(frozen=True)
class Principal:
    """Аутентифицированный автор запроса — то, что попадает в события.

    ``actor_type`` — тип держателя токена (USER/HERMES); ``actor_id`` — id токена:
    по журналу видно, каким именно токеном (устройством, агентом) сделано действие.
    """

    actor_type: str
    actor_id: uuid.UUID
    token_name: str


def generate_token() -> str:
    """Сгенерировать новый токен доступа (криптостойкий ГСЧ)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 токена (hex) — единственное, что хранится в БД.

    Соль не нужна: токен — 256 бит случайности, перебор и радужные таблицы
    бессмысленны. Детерминированный хэш позволяет искать токен по UNIQUE-индексу.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(token: str) -> bool:
    """Может ли строка быть токеном Novryn (дешёвая проверка до запроса в БД)."""
    return token.startswith(TOKEN_PREFIX) and len(token) <= _MAX_TOKEN_LENGTH
