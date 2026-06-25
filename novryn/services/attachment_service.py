"""Сервис вложений задач (PRD §6; ATCH-01..04).

``attach_resource`` создаёт вложение + attachment.attached (ATCH-01); тип валидируется
CHECK ``ck_attachments_type`` (ATCH-02). ``detach_resource`` — soft-delete (D-04/D-07) с
идемпотентным no-op (D-08). Повторное прикрепление — всегда НОВАЯ строка (новый id),
soft-deleted строка остаётся в истории (D-07). Чтение активных вложений — в
``dependency_read.get_resources`` (read-tier).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from novryn.db.ids import new_id
from novryn.db.models import Attachment
from novryn.domain.events import EventType
from novryn.repositories.uow import mutate_with_event


async def attach_resource(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    type: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    title: str | None = None,
    url: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Прикрепить ресурс к задаче + событие attachment.attached (ATCH-01).

    ``type`` ограничен enum CHECK ck_attachments_type (ATCH-02). Всегда создаёт НОВУЮ
    строку — soft-deleted вложения не воскрешаются (D-07).

    Returns:
        id созданного вложения.
    """
    att_id = new_id()

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            insert(Attachment).values(
                id=att_id,
                task_id=task_id,
                type=type,
                title=title,
                url=url,
                metadata_json=metadata_json,
            )
        )
        return att_id

    await mutate_with_event(
        session,
        entity_type="attachment",
        entity_id=att_id,
        event_type=EventType.ATTACHMENT_ATTACHED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Attachment, att_id),
    )
    return att_id


async def detach_resource(
    session: AsyncSession,
    *,
    attachment_id: uuid.UUID,
    actor_type: str,
    actor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Soft-delete вложения + attachment.detached; no-op если его нет/уже удалено (D-08).

    Returns:
        id soft-deleted вложения, либо None при no-op.
    """
    row = (
        await session.execute(
            select(Attachment)
            .where(Attachment.id == attachment_id)
            .where(Attachment.deleted_at.is_(None))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        await session.rollback()  # закрыть read-autobegin
        return None  # no-op, без события (D-08)
    await session.rollback()  # закрыть read-autobegin перед явным begin() UoW

    async def apply(s: AsyncSession) -> uuid.UUID:
        await s.execute(
            update(Attachment)
            .where(Attachment.id == attachment_id)
            .values(deleted_at=func.now())
        )
        return attachment_id

    await mutate_with_event(
        session,
        entity_type="attachment",
        entity_id=attachment_id,
        event_type=EventType.ATTACHMENT_DETACHED,
        actor_type=actor_type,
        actor_id=actor_id,
        apply=apply,
        load_row=lambda s: s.get(Attachment, attachment_id),
    )
    return attachment_id
