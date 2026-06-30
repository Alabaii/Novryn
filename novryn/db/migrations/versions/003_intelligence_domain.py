"""intelligence domain: focus versioning + memory/pattern upsert keys + russian FTS

Revision ID: 003
Revises: 002
Create Date: 2026-06-30

Рукописная миграция (план 03-01, Task 1), мирроринг 002: весь DDL — явный
``op.execute()`` (Alembic autogenerate не видит generated-колонки, частичные
индексы и DROP CONSTRAINT надёжно). Закладывает примитивы intelligence-домена
(FOCUS/MEM/PAT/INS) для доменных сервисов Фазы 3 (Wave 2).

Состав ``upgrade()`` (порядок важен):
 1. daily_focus.generated_at (TIMESTAMPTZ NOT NULL DEFAULT now()) + focus_set_id
    (UUID NOT NULL) — версионирование снимков (D-01/D-02). Версия = focus_set_id
    (один UUID на снимок), НЕ generated_at: равный generated_at двух снимков
    (один тик / NTP-сдвиг назад) смешал бы строки версий (CR-01).
 2. idx_daily_focus_date_gen — (date, generated_at DESC, focus_set_id DESC, rank,
    id DESC): детерминированный выбор последнего focus_set_id на дату + порядок строк.
 3. user_memory.memory_type → NOT NULL + uq_user_memory_type (UNIQUE) — ключ
    upsert (D-05: store того же типа обновляет строку in-place).
 4. behavior_patterns.pattern_type → NOT NULL + uq_behavior_pattern_type (UNIQUE) —
    ключ upsert поведенческих паттернов.
 5. user_memory.search_vector — STORED generated tsvector конфигом 'russian'
    (MEM-03). Выражение БАЙТ-В-БАЙТ совпадает с Computed в novryn/db/models.py
    (Pitfall 1 — иначе seq scan / рассинхрон схемы, тихая деградация поиска).
 6. idx_user_memory_search_vector — GIN по search_vector.

НЕ переобъявляет CHECK confidence (ck_user_memory_confidence/ck_behavior_confidence
уже существуют — D-08); диапазон 0.0–1.0 остаётся enforced на write (MEM-02).
НЕ добавляет deleted_at на focus/memory/pattern (версии/upsert вместо soft-delete —
D-01/D-05). Таблицы пусты на момент миграции → SET NOT NULL безопасен (RESEARCH
Runtime State Inventory).

``downgrade()`` реверсирует в обратном порядке (dev/тесты — как и в 001/002).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | Sequence[str] | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade: focus versioning + memory/pattern upsert keys + russian memory FTS."""
    # 1) daily_focus версионирование снимков (D-01/D-02). focus_set_id —
    #    непрозрачный ключ версии (один UUID на снимок); generated_at лишь
    #    упорядочивает снимки на дату. Выбор версии по focus_set_id, НЕ по
    #    равенству generated_at (CR-01: два снимка в одном тике / NTP-сдвиг
    #    назад дают равный generated_at и смешали бы строки двух версий).
    #    Таблица пуста на момент миграции → NOT NULL без backfill безопасен.
    op.execute(
        "ALTER TABLE daily_focus ADD COLUMN generated_at TIMESTAMPTZ "
        "NOT NULL DEFAULT now();"
    )
    # focus_set_id: добавляем nullable → backfill → SET NOT NULL (безопасно даже
    # если таблица не пуста — downgrade/upgrade-цикл в тестах оставляет строки
    # без колонки). Backfill даёт каждой осиротевшей строке собственный UUID
    # (она становится отдельной версией — приемлемо для остаточных строк).
    # Приложение ВСЕГДА проставляет focus_set_id явно (один на снимок), поэтому
    # постоянный column-default не нужен и не закладывается.
    op.execute("ALTER TABLE daily_focus ADD COLUMN focus_set_id UUID;")
    op.execute(
        "UPDATE daily_focus SET focus_set_id = gen_random_uuid() "
        "WHERE focus_set_id IS NULL;"
    )
    op.execute("ALTER TABLE daily_focus ALTER COLUMN focus_set_id SET NOT NULL;")
    # 2) Индекс под «последний снимок на дату»: упорядочивает версии по
    #    generated_at DESC, focus_set_id DESC (детерминированный «последний»
    #    focus_set_id даже при равном generated_at), затем строки по rank/id.
    op.execute(
        "CREATE INDEX idx_daily_focus_date_gen "
        "ON daily_focus (date, generated_at DESC, focus_set_id DESC, rank, id DESC);"
    )

    # 3) user_memory.memory_type → NOT NULL + UNIQUE (ключ upsert, D-05).
    op.execute("ALTER TABLE user_memory ALTER COLUMN memory_type SET NOT NULL;")
    op.execute(
        "ALTER TABLE user_memory "
        "ADD CONSTRAINT uq_user_memory_type UNIQUE (memory_type);"
    )

    # 4) behavior_patterns.pattern_type → NOT NULL + UNIQUE (ключ upsert).
    op.execute(
        "ALTER TABLE behavior_patterns ALTER COLUMN pattern_type SET NOT NULL;"
    )
    op.execute(
        "ALTER TABLE behavior_patterns "
        "ADD CONSTRAINT uq_behavior_pattern_type UNIQUE (pattern_type);"
    )

    # 5) user_memory.search_vector — STORED generated, конфиг 'russian'. Выражение
    #    БАЙТ-В-БАЙТ как Computed в models.py (Pitfall 1).
    op.execute(
        """
        ALTER TABLE user_memory ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector('russian', coalesce(content,''))
            ) STORED;
        """
    )
    # 6) GIN по search_vector.
    op.execute(
        "CREATE INDEX idx_user_memory_search_vector "
        "ON user_memory USING GIN (search_vector);"
    )


def downgrade() -> None:
    """Downgrade: реверс 003 (только dev/тесты), восстановление состояния 002."""
    # Обратный порядок: 6→5 (memory FTS), 4 (pattern key), 3 (memory key),
    # 2→1 (focus versioning).
    op.execute("DROP INDEX IF EXISTS idx_user_memory_search_vector;")
    op.execute("ALTER TABLE user_memory DROP COLUMN IF EXISTS search_vector;")

    op.execute(
        "ALTER TABLE behavior_patterns "
        "DROP CONSTRAINT IF EXISTS uq_behavior_pattern_type;"
    )
    op.execute(
        "ALTER TABLE behavior_patterns ALTER COLUMN pattern_type DROP NOT NULL;"
    )

    op.execute(
        "ALTER TABLE user_memory DROP CONSTRAINT IF EXISTS uq_user_memory_type;"
    )
    op.execute("ALTER TABLE user_memory ALTER COLUMN memory_type DROP NOT NULL;")

    op.execute("DROP INDEX IF EXISTS idx_daily_focus_date_gen;")
    op.execute("ALTER TABLE daily_focus DROP COLUMN IF EXISTS focus_set_id;")
    op.execute("ALTER TABLE daily_focus DROP COLUMN IF EXISTS generated_at;")
