"""Декларативная база SQLAlchemy 2.0 для всех ORM-моделей.

`Base.metadata` — единый источник metadata: его наследуют все доменные модели
(novryn/db/models.py) и он же служит `target_metadata` для Alembic env.py
(план 03). Стиль 2.0: `DeclarativeBase` + типизированные `Mapped`/`mapped_column`.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общий декларативный базовый класс для всех ORM-моделей Novryn."""
