"""soft-delete columns + partial unique index + russian FTS generated column

Revision ID: 002
Revises: 001
Create Date: 2026-06-25

Рукописная миграция (план 02-01, Task 3), мирроринг 001: весь DDL — явный
``op.execute()`` (Alembic autogenerate не видит generated-колонки, частичные
индексы и DROP CONSTRAINT надёжно). Закладывает примитивы для доменных сервисов
Фазы 2.

Состав ``upgrade()`` (порядок важен):
 1. DROP idx_tasks_fts — simple-config FTS-индекс из 001 (A9: оставлять оба
    конфига нельзя, рассинхрон → seq scan).
 2. tasks.search_vector — STORED generated tsvector конфигом 'russian'
    (TASK-09/D-14). Выражение БАЙТ-В-БАЙТ совпадает с Computed в
    novryn/db/models.py (Pitfall 1).
 3. idx_tasks_search_vector — GIN по search_vector.
 4. attachments.deleted_at (TIMESTAMPTZ NULL) — soft-delete detach (D-04/D-07).
 5. idx_attachments_active — частичный индекс активных вложений (deleted_at IS NULL).
 6. task_dependencies.deleted_at — soft-delete unlink (D-04/D-07).
 7. DROP CONSTRAINT uq_task_dependencies_pair — полный UNIQUE из 001.
 8. uq_task_dependencies_active — частичный UNIQUE только по активным рёбрам
    (D-06): повторная привязка ранее soft-deleted пары не нарушает уникальность.

``downgrade()`` реверсирует в обратном порядке (dev/тесты — как и в 001),
восстанавливая полный UNIQUE и simple-config idx_tasks_fts из 001.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | Sequence[str] | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade: soft-delete + partial unique + russian FTS generated column."""
    # 1) Снять simple-config FTS-индекс из 001 (заменяется russian search_vector).
    op.execute("DROP INDEX IF EXISTS idx_tasks_fts;")

    # 2) tasks.search_vector — STORED generated, конфиг 'russian'. Выражение
    #    БАЙТ-В-БАЙТ как Computed в models.py (Pitfall 1).
    op.execute(
        """
        ALTER TABLE tasks ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(description,''))
            ) STORED;
        """
    )
    # 3) GIN по search_vector.
    op.execute(
        "CREATE INDEX idx_tasks_search_vector ON tasks USING GIN (search_vector);"
    )

    # 4) attachments.deleted_at + 5) частичный индекс активных вложений.
    op.execute("ALTER TABLE attachments ADD COLUMN deleted_at TIMESTAMPTZ;")
    op.execute(
        "CREATE INDEX idx_attachments_active ON attachments (task_id) "
        "WHERE deleted_at IS NULL;"
    )

    # 6) task_dependencies.deleted_at + 7) DROP полного UNIQUE + 8) частичный UNIQUE.
    op.execute("ALTER TABLE task_dependencies ADD COLUMN deleted_at TIMESTAMPTZ;")
    op.execute(
        "ALTER TABLE task_dependencies DROP CONSTRAINT uq_task_dependencies_pair;"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_task_dependencies_active "
        "ON task_dependencies (task_id, depends_on_task_id) "
        "WHERE deleted_at IS NULL;"
    )


def downgrade() -> None:
    """Downgrade: реверс 002 (только dev/тесты), восстановление состояния 001."""
    # Обратный порядок: 8→7 (task_dependencies), 5→4 (attachments), 3→2 (tasks), 1.
    op.execute("DROP INDEX IF EXISTS uq_task_dependencies_active;")
    op.execute(
        "ALTER TABLE task_dependencies "
        "ADD CONSTRAINT uq_task_dependencies_pair UNIQUE (task_id, depends_on_task_id);"
    )
    op.execute("ALTER TABLE task_dependencies DROP COLUMN IF EXISTS deleted_at;")

    op.execute("DROP INDEX IF EXISTS idx_attachments_active;")
    op.execute("ALTER TABLE attachments DROP COLUMN IF EXISTS deleted_at;")

    op.execute("DROP INDEX IF EXISTS idx_tasks_search_vector;")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS search_vector;")
    # Восстановить simple-config idx_tasks_fts ровно как в 001 (146-152).
    op.execute(
        """
        CREATE INDEX idx_tasks_fts ON tasks
            USING GIN (to_tsvector('simple',
                coalesce(title, '') || ' ' || coalesce(description, '')));
        """
    )
