"""Генерация идентификаторов на базе UUID v7 (NFR-01).

Все id в системе — UUID v7 (time-ordered), что даёт локальность B-tree-индексов
для append-heavy таблиц (особенно `events`). Случайные (версии 4) UUID
запрещены — они фрагментируют индексы на 1M+ строк (CLAUDE.md "What NOT to Use").
"""

import uuid

import uuid_utils


def new_id() -> uuid.UUID:
    """Вернуть новый UUID v7.

    Делегирует Rust-реализации `uuid_utils.uuid7()` (`Uuid::now_v7()`). Все UUID,
    сгенерированные одним процессом через этот метод, гарантированно упорядочены
    по времени создания — это обеспечивает монотонность даже под конкурентной
    async-генерацией в одном процессе (NFR-01, success criterion #4).

    Кросс-процессная монотонность НЕ гарантируется и для single-process V1
    нерелевантна (RESEARCH §Summary п.4, Pitfall 6). Собственный timestamp+counter
    packer писать запрещено (RESEARCH "Don't Hand-Roll").

    Returns:
        Стандартный :class:`uuid.UUID` (версия 7), пригодный для прямого хранения
        в SQLAlchemy-колонках типа ``Uuid``. Значение `uuid_utils.uuid7()`
        приводится к stdlib-типу по байтам, чтобы декларированный тип возврата был
        честным под `mypy --strict` (downstream ORM ожидает именно `uuid.UUID`).
    """
    return uuid.UUID(bytes=bytes(uuid_utils.uuid7().bytes))
