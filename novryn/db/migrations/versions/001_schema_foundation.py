"""schema foundation: 8 tables + events partitioning + append-only + REVOKE + CHECK

Revision ID: 001
Revises:
Create Date: 2026-06-24

Рукописная миграция (план 01-03, Task 2). НЕ autogenerate-черновик: Alembic
autogenerate не видит партиционирование, триггеры/функции, REVOKE/GRANT и
(надёжно) CHECK (RESEARCH Pattern 6). Поэтому весь DDL написан явно через
``op.execute()``.

Состав ``upgrade()`` (порядок важен):
 1. app-роль ``novryn_app`` (NOLOGIN) через идемпотентный DO-блок — нужна, чтобы
    REVOKE был осмысленным (RESEARCH Open Q1 / Pitfall 5: REVOKE декоративен, если
    приложение = owner таблицы). D-06 рубеж 2.
 2. 7 доменных таблиц с VARCHAR+CHECK enum (D-08; канон значений — PRD/REQUIREMENTS,
    верхний регистр) и confidence BETWEEN 0 AND 1. Имена CHECK совпадают с моделями
    (novryn/db/models.py) — план 03 их проверяет.
 3. ``events`` как ``PARTITION BY RANGE (occurred_at)`` c составным
    ``PRIMARY KEY (id, occurred_at)`` (PK ОБЯЗАН включать ключ партиции — Pitfall 2).
 4. Индекс ``idx_events_entity`` на родителе (авто-распространяется на партиции).
 5. >=12 месячных партиций ``events_YYYY_MM`` (с 2026-06 вперёд) + ``events_default``
    (D-10 страховка: INSERT за горизонтом не падает — Pitfall 3).
 6. Триггерная функция ``events_block_mutation()`` + триггер
    ``trg_events_append_only BEFORE UPDATE OR DELETE`` на родителе (PG13+ авто-клон
    на партиции — D-06 рубеж 1). Защищает даже owner.
 7. ``REVOKE UPDATE, DELETE ON events FROM novryn_app`` + ``GRANT SELECT, INSERT``
    (D-06 рубеж 2). GRANT SELECT/INSERT на доменные таблицы той же роли для
    интеграционного теста append-only под ``SET ROLE novryn_app``.

``downgrade()`` дропает в обратном порядке (полный — нужен для пересоздания в
тестах). ПОЛИТИКА: применять downgrade к event store в проде ЗАПРЕЩЕНО (это
уничтожает аудит) — RESEARCH Open Q3. Здесь реализован только для dev/тестов.

Требование среды: PostgreSQL >= 13 (фактически 16). На PG <= 12 BEFORE-триггер на
партиционированном родителе невозможен (Pitfall 4 / A3).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Канонические enum-множества — синхронны с novryn/db/models.py (PRD/REQUIREMENTS).
_TASK_STATUS = ("INBOX", "TODO", "IN_PROGRESS", "BLOCKED", "DONE", "ARCHIVED")
_TASK_ENERGY = ("LOW", "MEDIUM", "HIGH")
_ATTACHMENT_TYPE = ("URL", "DOCUMENT", "PDF", "GITHUB", "GOOGLE_DOC", "OTHER")
_SESSION_RESULT = ("COMPLETED", "PARTIAL", "ABANDONED", "INTERRUPTED")
_EVENT_ACTOR_TYPE = ("USER", "HERMES", "SYSTEM")

# Старт горизонта месячных партиций (месяц планирования) и их число (D-10).
_PARTITION_START_YEAR = 2026
_PARTITION_START_MONTH = 6
_PARTITION_COUNT = 12

# Доменные таблицы в порядке создания (для GRANT и downgrade-дропа).
_DOMAIN_TABLES = (
    "tasks",
    "task_dependencies",
    "attachments",
    "sessions",
    "daily_focus",
    "user_memory",
    "behavior_patterns",
)


def _in_sql(column: str, values: tuple[str, ...]) -> str:
    """Построить SQL-фрагмент ``column IN ('A','B',...)`` для CHECK."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _month_bounds(index: int) -> tuple[str, str]:
    """Вернуть (FROM, TO) границы месяца index относительно старта горизонта.

    Границы — первое число месяца; диапазон полуинтервал [FROM, TO).
    """
    month0 = (_PARTITION_START_YEAR * 12 + (_PARTITION_START_MONTH - 1)) + index
    from_year, from_month = divmod(month0, 12)
    to_year, to_month = divmod(month0 + 1, 12)
    return (
        f"{from_year:04d}-{from_month + 1:02d}-01",
        f"{to_year:04d}-{to_month + 1:02d}-01",
    )


def _partition_name(index: int) -> str:
    """Имя месячной партиции events_YYYY_MM для смещения index."""
    month0 = (_PARTITION_START_YEAR * 12 + (_PARTITION_START_MONTH - 1)) + index
    year, month = divmod(month0, 12)
    return f"events_{year:04d}_{month + 1:02d}"


def upgrade() -> None:
    """Upgrade schema: tables + partitions + append-only + REVOKE + CHECK."""
    # 1) app-роль novryn_app (NOLOGIN). CREATE ROLE не поддерживает IF NOT EXISTS —
    #    идемпотентность через DO-блок (Open Q1 / Pitfall 5).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novryn_app') THEN
                CREATE ROLE novryn_app NOLOGIN;
            END IF;
        END
        $$;
        """
    )

    # 2) Доменные таблицы. Enum = VARCHAR + именованный CHECK (D-08); confidence
    #    BETWEEN 0 AND 1. Имена CHECK совпадают с моделями.
    op.execute(
        f"""
        CREATE TABLE tasks (
            id                          UUID PRIMARY KEY,
            title                       TEXT NOT NULL,
            description                 TEXT,
            status                      VARCHAR(20) NOT NULL DEFAULT 'INBOX',
            due_date                    TIMESTAMPTZ,
            parent_task_id              UUID REFERENCES tasks(id),
            user_time_estimate_minutes  INTEGER,
            ai_time_estimate_minutes    INTEGER,
            energy_required             VARCHAR(10),
            blocked_reason              TEXT,
            ai_context_json             JSONB,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                  TIMESTAMPTZ DEFAULT now(),
            completed_at                TIMESTAMPTZ,
            archived_at                 TIMESTAMPTZ,
            CONSTRAINT ck_tasks_status CHECK ({_in_sql("status", _TASK_STATUS)}),
            CONSTRAINT ck_tasks_energy CHECK ({_in_sql("energy_required", _TASK_ENERGY)})
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_tasks_parent_task_id ON tasks (parent_task_id);"
    )
    # FTS-GIN под будущий поиск (Фаза 2/5) — дёшево, задел; не обязателен для Фазы 1.
    op.execute(
        """
        CREATE INDEX idx_tasks_fts ON tasks
            USING GIN (to_tsvector('simple',
                coalesce(title, '') || ' ' || coalesce(description, '')));
        """
    )

    op.execute(
        """
        CREATE TABLE task_dependencies (
            id                  UUID PRIMARY KEY,
            task_id             UUID NOT NULL REFERENCES tasks(id),
            depends_on_task_id  UUID NOT NULL REFERENCES tasks(id),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_task_dep_no_self CHECK (task_id <> depends_on_task_id),
            CONSTRAINT uq_task_dependencies_pair UNIQUE (task_id, depends_on_task_id)
        );
        """
    )

    op.execute(
        f"""
        CREATE TABLE attachments (
            id              UUID PRIMARY KEY,
            task_id         UUID NOT NULL REFERENCES tasks(id),
            type            VARCHAR(20) NOT NULL,
            title           TEXT,
            url             TEXT,
            metadata_json   JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_attachments_type CHECK ({_in_sql("type", _ATTACHMENT_TYPE)})
        );
        """
    )

    op.execute(
        f"""
        CREATE TABLE sessions (
            id              UUID PRIMARY KEY,
            task_id         UUID NOT NULL REFERENCES tasks(id),
            planned_minutes INTEGER,
            actual_minutes  INTEGER,
            started_at      TIMESTAMPTZ,
            ended_at        TIMESTAMPTZ,
            result          VARCHAR(20),
            notes           TEXT,
            CONSTRAINT ck_sessions_result CHECK ({_in_sql("result", _SESSION_RESULT)})
        );
        """
    )

    op.execute(
        """
        CREATE TABLE daily_focus (
            id              UUID PRIMARY KEY,
            date            DATE NOT NULL,
            task_id         UUID NOT NULL REFERENCES tasks(id),
            rank            INTEGER NOT NULL,
            reason          TEXT,
            generated_by    TEXT
        );
        """
    )

    op.execute(
        """
        CREATE TABLE user_memory (
            id          UUID PRIMARY KEY,
            memory_type TEXT,
            content     TEXT,
            confidence  NUMERIC(3, 2),
            source      TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_user_memory_confidence
                CHECK (confidence BETWEEN 0.0 AND 1.0)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE behavior_patterns (
            id              UUID PRIMARY KEY,
            pattern_type    TEXT,
            confidence      NUMERIC(3, 2),
            evidence_json   JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT ck_behavior_confidence
                CHECK (confidence BETWEEN 0.0 AND 1.0)
        );
        """
    )

    # 3) events — партиционированная append-only аудит-таблица. PK ОБЯЗАН включать
    #    occurred_at (ключ партиции) — Pitfall 2. Все 8 обязательных полей +
    #    schema_version DEFAULT 1 (EVT-04) + payload_json JSONB DEFAULT '{}'.
    op.execute(
        f"""
        CREATE TABLE events (
            id              UUID NOT NULL,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            schema_version  SMALLINT NOT NULL DEFAULT 1,
            event_type      TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            entity_id       UUID NOT NULL,
            actor_type      VARCHAR(10) NOT NULL,
            actor_id        UUID,
            payload_json    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            CONSTRAINT ck_events_actor_type
                CHECK ({_in_sql("actor_type", _EVENT_ACTOR_TYPE)}),
            PRIMARY KEY (id, occurred_at)
        ) PARTITION BY RANGE (occurred_at);
        """
    )

    # 4) Индекс на родителе → авто-распространяется на все партиции (PG11+).
    op.execute(
        """
        CREATE INDEX idx_events_entity ON events
            (entity_type, entity_id, occurred_at DESC);
        """
    )

    # 5) >=12 месячных партиций + DEFAULT-страховка (D-10).
    for i in range(_PARTITION_COUNT):
        name = _partition_name(i)
        lo, hi = _month_bounds(i)
        op.execute(
            f"CREATE TABLE {name} PARTITION OF events "
            f"FOR VALUES FROM ('{lo}') TO ('{hi}');"
        )
    op.execute("CREATE TABLE events_default PARTITION OF events DEFAULT;")

    # 6) Append-only рубеж 1: триггерная функция + BEFORE UPDATE OR DELETE на
    #    родителе (PG13+ авто-клонирует на партиции). Защищает даже owner.
    op.execute(
        """
        CREATE FUNCTION events_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'events is append-only: % on events is forbidden', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_events_append_only
            BEFORE UPDATE OR DELETE ON events
            FOR EACH ROW EXECUTE FUNCTION events_block_mutation();
        """
    )

    # 7) Append-only рубеж 2: REVOKE под novryn_app + GRANT SELECT,INSERT.
    op.execute("REVOKE UPDATE, DELETE ON events FROM novryn_app;")
    op.execute("GRANT SELECT, INSERT ON events TO novryn_app;")
    # GRANT на доменные таблицы той же роли — для интеграционного append-only-теста
    # под SET ROLE novryn_app (вставка события + попытка UPDATE/DELETE).
    for table in _DOMAIN_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO novryn_app;")


def downgrade() -> None:
    """Downgrade schema (только dev/тесты — в проде downgrade аудита ЗАПРЕЩЁН).

    Дропаем в обратном порядке: триггер → функция → events (каскадом партиции и
    индекс) → доменные таблицы → роль.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_events_append_only ON events;")
    op.execute("DROP FUNCTION IF EXISTS events_block_mutation();")
    # DROP TABLE events каскадно убирает все партиции и idx_events_entity.
    op.execute("DROP TABLE IF EXISTS events CASCADE;")

    for table in reversed(_DOMAIN_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novryn_app') THEN
                DROP ROLE novryn_app;
            END IF;
        END
        $$;
        """
    )
