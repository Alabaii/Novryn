"""Сервис иерархии задач: дерево поддерева одним запросом + guard глубины (HIER-02/03).

``get_task_tree`` — ROADMAP-критерий #2: поддерево извлекается ОДНИМ рекурсивным CTE
(no N+1, доказывается счётчиком запросов), вложенность собирается в памяти (D-11 — не
нарушает «один запрос», т.к. IO один). Превышение настраиваемого предела глубины —
ЯВНАЯ ``DepthExceededError`` (D-13/HIER-03), а не тихое усечение. Read-сторона:
событий НЕ пишет.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from novryn.config import get_tree_max_depth
from novryn.domain.errors import DepthExceededError, NotFoundError
from novryn.repositories.task_read import fetch_subtree_rows


async def get_task_tree(session: AsyncSession, root_id: uuid.UUID) -> dict[str, Any]:
    """Вернуть вложенное дерево поддерева root_id (HIER-01/02/03, D-11/D-13).

    Запрашивает поддерево одним CTE с лимитом ``max_depth + 1`` — лишний уровень нужен,
    чтобы ОТЛИЧИТЬ «глубже лимита» от «ровно на лимите». Если строк нет —
    ``NotFoundError``. Если есть узел глубже лимита — ``DepthExceededError`` (НЕ усечение,
    D-13). Иначе собирает вложенную структуру (root с ``children``, рекурсивно) в памяти.
    ARCHIVED-подзадачи включены (D-13).

    Returns:
        dict-узел корня: ``{id, parent_task_id, title, status, children: [...]}``.

    Raises:
        NotFoundError: если задачи root_id нет.
        DepthExceededError: если поддерево глубже настроенного предела (D-13/HIER-03).
    """
    max_depth = get_tree_max_depth()
    rows = await fetch_subtree_rows(session, root_id, max_depth + 1)
    if not rows:
        raise NotFoundError(root_id)
    if any(r["depth"] > max_depth for r in rows):
        raise DepthExceededError(root_id, max_depth)

    by_id: dict[uuid.UUID, dict[str, Any]] = {
        r["id"]: {
            "id": r["id"],
            "parent_task_id": r["parent_task_id"],
            "title": r["title"],
            "status": r["status"],
            "children": [],
        }
        for r in rows
    }

    root_node: dict[str, Any] | None = None
    for r in rows:
        node = by_id[r["id"]]
        if r["depth"] == 0:
            root_node = node
        else:
            parent = by_id.get(r["parent_task_id"])
            if parent is not None:
                parent["children"].append(node)

    assert root_node is not None  # depth==0 строка гарантирована (rows непуст, корень есть)
    return root_node
