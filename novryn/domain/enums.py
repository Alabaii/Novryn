"""Канонические перечисления домена (PRD §4–§7) — единый источник истины.

Значения в верхнем регистре. БД держит их как VARCHAR + именованный CHECK (D-08):
ORM-модели строят CHECK из этих же типов (``typing.get_args``), MCP-слой публикует их в
схемах инструментов. Миграции хранят СВОИ копии значений: миграция — снимок истории и
не должна меняться вместе с кодом приложения.

Чистый доменный слой: только ``typing``.
"""

from typing import Literal

TaskStatus = Literal["INBOX", "TODO", "IN_PROGRESS", "BLOCKED", "DONE", "ARCHIVED"]
TaskEnergy = Literal["LOW", "MEDIUM", "HIGH"]
AttachmentType = Literal["URL", "DOCUMENT", "PDF", "GITHUB", "GOOGLE_DOC", "OTHER"]
SessionResult = Literal["COMPLETED", "PARTIAL", "ABANDONED", "INTERRUPTED"]
