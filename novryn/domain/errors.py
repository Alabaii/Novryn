"""Доменные типы ошибок для сервисов Фазы 2 (D-13, DEP-04, DEP-05, HIER-03).

Чистый доменный слой: НИЧЕГО не импортирует из SQLAlchemy. Сервисы Фазы 2
(task_service, hierarchy_service, dependency_service) поднимают эти ошибки по
бизнес-смыслу; MCP-слой Фазы 4 транслирует их в protocol-ошибки на границе.

Каждая ошибка хранит свои аргументы как атрибуты (для структурного логирования и
последующей трансляции) и формирует понятное сообщение. Привязка к решениям:
- NotFoundError       — целевой строки нет (get/update/complete/archive); общая доменная ошибка.
- DepthExceededError  — поддерево глубже предела; ЯВНАЯ ошибка, НЕ тихое усечение (D-13/HIER-03).
- CycleError          — новое ребро зависимости образует цикл (DEP-05).
- SelfDependencyError — задача зависит от самой себя (DEP-04).
"""

from __future__ import annotations

import uuid


class DomainError(Exception):
    """База доменных ошибок Novryn — единая точка перехвата на границе сервис→MCP."""


class NotFoundError(DomainError):
    """Запрошенной сущности не существует (get/update/complete/archive)."""

    def __init__(self, entity_id: uuid.UUID, entity_type: str = "task") -> None:
        self.entity_id = entity_id
        self.entity_type = entity_type
        super().__init__(f"{entity_type} {entity_id} не найдена")


class DepthExceededError(DomainError):
    """Поддерево превысило настраиваемый предел глубины (D-13/HIER-03).

    Поднимается get_task_tree ВМЕСТО тихого усечения: потребитель должен знать, что
    дерево обрезано пределом, а не закончилось естественно.
    """

    def __init__(self, root_id: uuid.UUID, max_depth: int) -> None:
        self.root_id = root_id
        self.max_depth = max_depth
        super().__init__(
            f"дерево задачи {root_id} превышает максимальную глубину {max_depth}"
        )


class CycleError(DomainError):
    """Новое ребро зависимости образовало бы цикл (DEP-05)."""

    def __init__(self, task_id: uuid.UUID, depends_on_task_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.depends_on_task_id = depends_on_task_id
        super().__init__(
            f"зависимость {task_id} → {depends_on_task_id} образует цикл"
        )


class SelfDependencyError(DomainError):
    """Задача не может зависеть от самой себя (DEP-04)."""

    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        super().__init__(f"задача {task_id} не может зависеть от самой себя")
