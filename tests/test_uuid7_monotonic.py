"""Тесты генерации UUID v7 и доказательство монотонности (NFR-01, success criterion #4).

Критерий #4: 100 идентификаторов, сгенерированных конкурентными async-корутинами
в ОДНОМ процессе, монотонно возрастают по байтовому (`.int`) порядку.

Монотонность гарантируется per-process (Rust `Uuid::now_v7()`); кросс-процессная
монотонность НЕ проверяется и для single-process V1 нерелевантна (RESEARCH Pitfall 6).
"""

import asyncio
import uuid

from novryn.db.ids import new_id


async def _gen_one() -> int:
    """Вернуть `.int` нового UUID v7 (= байтовый порядок для сравнения)."""
    return new_id().int


async def test_uuid7_monotonic_under_concurrency() -> None:
    """100 конкурентных корутин в одном процессе → строго упорядоченные id."""
    ids = await asyncio.gather(*(_gen_one() for _ in range(100)))
    assert ids == sorted(ids), "UUID v7 must be monotonic per-process"


def test_sequential_ids_are_ordered() -> None:
    """Два последовательных вызова дают разные значения; второй >= первого."""
    first = new_id()
    second = new_id()
    assert first != second
    assert second.int >= first.int


def test_new_id_is_uuid_compatible() -> None:
    """new_id() возвращает объект, совместимый с uuid.UUID (.int и 16 байт)."""
    value = new_id()
    assert hasattr(value, "int")
    assert len(value.bytes) == 16
    # Должен быть приводим к stdlib uuid.UUID без потерь.
    assert uuid.UUID(int=value.int).int == value.int
    # UUID v7: версия в старшем полупайте 7-го байта.
    assert (value.bytes[6] >> 4) == 7
