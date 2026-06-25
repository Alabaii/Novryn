"""Интеграция (Фаза 2, план 04 / HIER-02/03): get_task_tree — один CTE + guard глубины.

- test_single_query: всё поддерево извлекается РОВНО одним SQL-запросом (счётчик == 1,
  no N+1, HIER-02) независимо от числа узлов;
- вложенная структура root→children (рекурсивно), ARCHIVED-подзадачи включены (D-11/D-13);
- test_depth_guard: цепочка глубже предела → DepthExceededError, НЕ усечение (D-13/HIER-03);
- неизвестный корень → NotFoundError.

Сидинг прямыми insert'ами; зависит от ``migrated_db``. Счётчик слушает
``async_engine.sync_engine`` (Pitfall 3) через ``count_queries`` из conftest.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from novryn.db.ids import new_id
from novryn.db.models import Task
from novryn.domain.errors import DepthExceededError, NotFoundError
from novryn.services import hierarchy_service
from tests.integration.conftest import count_queries


@pytest_asyncio.fixture
async def uow_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> async_sessionmaker[AsyncSession]:
    return sessionmaker


async def _insert(
    s: AsyncSession,
    *,
    title: str,
    status: str = "TODO",
    parent_task_id: uuid.UUID | None = None,
) -> uuid.UUID:
    tid = new_id()
    await s.execute(
        insert(Task).values(
            id=tid, title=title, status=status, parent_task_id=parent_task_id
        )
    )
    return tid


@pytest.mark.asyncio
async def test_single_query(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
) -> None:
    """3-уровневое поддерево с несколькими узлами извлекается РОВНО одним запросом."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            root = await _insert(s, title="root")
            c1 = await _insert(s, title="c1", parent_task_id=root)
            await _insert(s, title="c2", parent_task_id=root)
            await _insert(s, title="gc1", parent_task_id=c1)

    async with uow_sessionmaker() as session:
        with count_queries(async_engine) as counter:
            tree = await hierarchy_service.get_task_tree(session, root)

    assert counter["n"] == 1  # ровно один SQL-запрос — нет N+1 (HIER-02)
    assert tree["id"] == root
    assert len(tree["children"]) == 2  # c1, c2


@pytest.mark.asyncio
async def test_nested_structure_includes_archived(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Дерево вложенное; ARCHIVED-подзадача присутствует (D-11/D-13)."""
    async with uow_sessionmaker() as s:
        async with s.begin():
            root = await _insert(s, title="root2")
            await _insert(s, title="active child", parent_task_id=root)
            await _insert(s, title="archived child", status="ARCHIVED", parent_task_id=root)

    async with uow_sessionmaker() as session:
        tree = await hierarchy_service.get_task_tree(session, root)

    statuses = sorted(c["status"] for c in tree["children"])
    assert statuses == ["ARCHIVED", "TODO"]  # ARCHIVED включён (D-13)


@pytest.mark.asyncio
async def test_depth_guard_raises(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Цепочка глубже предела → DepthExceededError (НЕ усечение, D-13/HIER-03)."""
    monkeypatch.setenv("NOVRYN_TREE_MAX_DEPTH", "2")  # лимит 2 уровня
    async with uow_sessionmaker() as s:
        async with s.begin():
            root = await _insert(s, title="d-root")
            a = await _insert(s, title="d1", parent_task_id=root)
            b = await _insert(s, title="d2", parent_task_id=a)
            await _insert(s, title="d3", parent_task_id=b)  # depth 3 > 2

    with pytest.raises(DepthExceededError):
        async with uow_sessionmaker() as session:
            await hierarchy_service.get_task_tree(session, root)


@pytest.mark.asyncio
async def test_unknown_root_raises_not_found(
    uow_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(NotFoundError):
        async with uow_sessionmaker() as session:
            await hierarchy_service.get_task_tree(session, new_id())
