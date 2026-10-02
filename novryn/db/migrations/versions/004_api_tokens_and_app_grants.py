"""api tokens + app role grants: токены доступа к MCP и права novryn_app на UPDATE

Revision ID: 004
Revises: 003
Create Date: 2026-10-02

Рукописная миграция (план 04-02), как 001–003: весь DDL — явный ``op.execute()``
(autogenerate не видит GRANT/REVOKE и column-level привилегии).

Состав ``upgrade()``:
 1. ``api_tokens`` — токены доступа к MCP-эндпоинту. Хранится только SHA-256 хэш
    (UNIQUE — по нему идёт поиск); держатель — USER или HERMES (CHECK); отзыв —
    ``revoked_at``, строки не удаляются. Зеркало ``ApiToken`` в models.py.
 2. ``novryn_app`` получает SELECT/INSERT на api_tokens и UPDATE ТОЛЬКО колонки
    ``revoked_at``: приложение может отозвать токен, но не подменить его хэш или
    держателя (защита от эскалации USER → HERMES даже при ошибке в коде).
 3. ``novryn_app`` получает UPDATE на доменные таблицы, которые сервисы обновляют на
    месте (правка задачи, soft-delete связей, завершение сессии, upsert памяти и
    паттернов). Без этого сервер мог работать только под владельцем таблиц, а для
    владельца REVOKE на журнале декоративен (Pitfall 5 из 001).
    ``daily_focus`` (версии снимков) и ``events`` (журнал) остаются без UPDATE.
    DELETE не выдаётся нигде: удаление в системе только мягкое (deleted_at).

Сама LOGIN-роль сервера (член ``novryn_app``) здесь НЕ создаётся: её пароль — секрет
эксплуатации, ему не место в миграции. Её заводят при развёртывании (план 04-06):
``CREATE ROLE novryn_server LOGIN PASSWORD '...' IN ROLE novryn_app;``

``downgrade()`` реверсирует в обратном порядке (dev/тесты — как и в 001–003).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | Sequence[str] | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Синхронно с _TOKEN_HOLDER_TYPE в novryn/db/models.py.
_TOKEN_HOLDER_TYPE = ("USER", "HERMES")

# Таблицы, которые сервисы обновляют на месте. daily_focus и events — только INSERT.
_UPDATABLE_TABLES = (
    "tasks",
    "task_dependencies",
    "attachments",
    "sessions",
    "user_memory",
    "behavior_patterns",
)


def upgrade() -> None:
    """Upgrade: api_tokens + права novryn_app на токены и UPDATE доменных таблиц."""
    holders = ", ".join(f"'{h}'" for h in _TOKEN_HOLDER_TYPE)
    # 1) Токены доступа: только хэш, держатель USER/HERMES, отзыв без удаления.
    op.execute(
        f"""
        CREATE TABLE api_tokens (
            id          UUID PRIMARY KEY,
            name        TEXT NOT NULL,
            holder_type VARCHAR(10) NOT NULL,
            token_hash  VARCHAR(64) NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at  TIMESTAMPTZ,
            CONSTRAINT ck_api_tokens_holder_type CHECK (holder_type IN ({holders})),
            CONSTRAINT uq_api_tokens_token_hash UNIQUE (token_hash)
        );
        """
    )

    # 2) Приложение выдаёт и отзывает токены, но не меняет хэш и держателя.
    op.execute("GRANT SELECT, INSERT ON api_tokens TO novryn_app;")
    op.execute("GRANT UPDATE (revoked_at) ON api_tokens TO novryn_app;")

    # 3) UPDATE доменных таблиц, которые сервисы обновляют на месте.
    for table in _UPDATABLE_TABLES:
        op.execute(f"GRANT UPDATE ON {table} TO novryn_app;")


def downgrade() -> None:
    """Downgrade: реверс 004 (только dev/тесты), восстановление прав 003."""
    for table in reversed(_UPDATABLE_TABLES):
        op.execute(f"REVOKE UPDATE ON {table} FROM novryn_app;")
    # DROP TABLE снимает и все привилегии на api_tokens.
    op.execute("DROP TABLE IF EXISTS api_tokens;")
