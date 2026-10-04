"""Типы параметров инструментов: ограничения и описания попадают во входную схему.

Схему видит модель — описания отвечают на её вопрос «что сюда передать». Проверку
делает SDK до вызова инструмента: неверный UUID, время без часового пояса или слишком
большая страница возвращаются модели ошибкой, до БД не доходя.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from pydantic import Field

DEFAULT_LIMIT = 20
# has_more узнаём, запросив limit + 1 запись: MAX_LIMIT + 1 обязан не превышать потолок
# выдачи репозиториев (200), иначе лишняя запись срежется и has_more соврёт.
MAX_LIMIT = 100

TaskId = Annotated[uuid.UUID, Field(description="id задачи (UUID).")]

Limit = Annotated[
    int, Field(ge=1, le=MAX_LIMIT, description=f"Размер страницы, 1–{MAX_LIMIT}.")
]
Offset = Annotated[
    int,
    Field(ge=0, description="Сколько записей пропустить — next_offset прошлой страницы."),
]
MinConfidence = Annotated[
    float | None,
    Field(ge=0, le=1, description="Не ниже этой уверенности, 0.0–1.0."),
]
FocusDate = Annotated[
    datetime.date | None,
    Field(description="Дата ГГГГ-ММ-ДД; по умолчанию — сегодня в часовом поясе пользователя."),
]

# Сущности журнала, историю которых можно читать через MCP. Синхронно с entity_type,
# который пишут доменные сервисы. Токены доступа (api_token) сюда не входят: это
# дело оператора, а не агента.
EntityType = Literal[
    "task",
    "dependency",
    "attachment",
    "session",
    "daily_focus",
    "user_memory",
    "behavior_patterns",
]
