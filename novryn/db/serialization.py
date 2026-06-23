"""Единый сериализатор строки сущности в JSON-снимок для payload (D-02/D-03).

Назначение: дать ОДНУ функцию, которая превращает строку сущности (ORM-объект,
SQLAlchemy ``Row``/``RowMapping`` или обычный ``Mapping``) в JSON-совместимый
``dict`` ОДИНАКОВОЙ формы для `before` и `after`. Идентичность формы критична:
иначе diff между снимками станет ненадёжным (01-RESEARCH.md Pattern 2). Это
заменяет ручной per-поле разбор в каждом сервисе ("Don't Hand-Roll").

Правила нормализации (зафиксированы — применять ВЕЗДЕ одинаково):
- ``uuid.UUID``           → ``str`` (каноническая форма).
- ``datetime``/``date``   → ISO-8601 ``str`` (TIMESTAMPTZ несёт tzinfo).
- ``decimal.Decimal``     → ``str`` (БЕЗ потери точности; confidence ∈ [0,1]).
  Выбран ``str``, а не ``float``: float исказил бы NUMERIC(3,2). Фиксируем ОДНО
  представление для before/after (01-RESEARCH.md Pattern 2).
- JSONB-поля (``ai_context_json``/``metadata_json``/``evidence_json``) — это уже
  ``dict``/``list``/скаляр; вкладываются ЦЕЛИКОМ как есть (D-03, опора на TOAST),
  но рекурсивно нормализуются (на случай вложенных UUID/datetime).
- ``None`` сохраняется как ``None`` (на CREATE before=None задаёт UoW — сюда не
  попадает; здесь None — это NULL-значение поля).

На отсутствующей строке (CREATE) сериализатор НЕ вызывается: before=None
проставляет UoW (novryn/repositories/uow.py).
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Mapper


def _to_jsonable(value: Any) -> Any:
    """Рекурсивно нормализовать значение в JSON-совместимое представление.

    Единые правила (см. модульный docstring). Применяется и к скалярам колонок,
    и к содержимому JSONB-полей, чтобы вложенные UUID/datetime тоже стали
    строками — иначе ``json.dumps`` payload упадёт.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):  # date после datetime: datetime — подтип date
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    # Неизвестный тип — приводим к строке как защитный дефолт (форма стабильна).
    return str(value)


def serialize_row(row: object) -> dict[str, Any]:
    """Сериализовать строку сущности в JSON-совместимый ``dict`` (D-02).

    Принимает:
    - ORM-инстанс (любой ``Base``-наследник) → берутся ВСЕ маппированные колонки
      (полная строка, D-02), включая server-default'ы, материализованные после
      ``flush`` (UoW делает flush до снимка `after`);
    - ``Mapping``/``RowMapping`` (например результат ``.mappings().one()``) →
      берутся все ключи как есть.

    Форма результата одинакова для before/after — это и есть смысл единого
    сериализатора (Pattern 2). Значения нормализуются ``_to_jsonable``.

    Raises:
        TypeError: если ``row`` не ORM-инстанс и не ``Mapping``.
    """
    if isinstance(row, Mapping):
        return {str(k): _to_jsonable(v) for k, v in row.items()}

    mapper: Mapper[Any] | None = inspect(type(row), raiseerr=False)
    if mapper is not None and isinstance(mapper, Mapper):
        return {
            attr.key: _to_jsonable(getattr(row, attr.key))
            for attr in mapper.column_attrs
        }

    raise TypeError(
        f"serialize_row ожидает ORM-инстанс или Mapping, получено {type(row)!r}"
    )
