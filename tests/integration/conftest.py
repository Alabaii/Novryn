"""Интеграционные хелперы Фазы 2 (поверх session-scoped фикстур tests/conftest.py).

``count_queries`` — счётчик SQL-исполнений для доказательства отсутствия N+1
(HIER-02, тест дерева в плане 04). КРИТИЧНО (Pitfall 3): listener вешается на
``async_engine.sync_engine``, НЕ на ``async_engine`` — у async-движка нет события
``before_cursor_execute`` напрямую, и счётчик молча остался бы в нуле (тест «одна
запрос» прошёл бы ложно). Здесь же он живёт, чтобы соседние интеграционные тесты
импортировали его как ``from tests.integration.conftest import count_queries``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


@contextmanager
def count_queries(async_engine: AsyncEngine) -> Iterator[dict[str, int]]:
    """Считать число SQL cursor-исполнений внутри блока ``with`` (no-N+1 guard).

    Вешает ``before_cursor_execute`` на синхронный движок под async-обёрткой
    (``async_engine.sync_engine`` — Pitfall 3) и инкрементит счётчик на каждом
    реальном исполнении курсора. Снимает listener в ``finally``.

    Использование::

        with count_queries(async_engine) as counter:
            await service.get_task_tree(session, root_id)
        assert counter["n"] == 1  # ровно один запрос — нет N+1

    Args:
        async_engine: session-scoped async-движок (фикстура tests/conftest.py).

    Yields:
        dict ``{"n": <число исполнений>}``; читать ПОСЛЕ выхода из блока with.
    """
    counter: dict[str, int] = {"n": 0}
    sync_engine = async_engine.sync_engine

    def _on_exec(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        counter["n"] += 1

    event.listen(sync_engine, "before_cursor_execute", _on_exec)
    try:
        yield counter
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_exec)
