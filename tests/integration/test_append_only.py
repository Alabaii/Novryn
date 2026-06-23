"""Интеграция (критерий #5 / EVT-03/EVT-04): events append-only.

Проверяем:
- INSERT события успешен; schema_version по умолчанию = 1 (EVT-04);
- UPDATE по events → исключение (триггер рубеж 1, защищает даже owner);
- DELETE по events → исключение (триггер рубеж 1);
- рубеж 2 (REVOKE под novryn_app): право UPDATE/DELETE отозвано в каталоге;
  под `SET ROLE novryn_app` UPDATE/DELETE также отвергаются (skip, если роль
  недоступна).

Свежая сессия на тест; зависит от `migrated_db`.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novryn.db.ids import new_id


@pytest_asyncio.fixture
async def msession(
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


async def _insert_event(session: AsyncSession) -> uuid.UUID:
    event_id = new_id()
    await session.execute(
        text(
            "INSERT INTO events "
            "(id, event_type, entity_type, entity_id, actor_type) "
            "VALUES (:id, 'test.created', 'task', :eid, 'SYSTEM')"
        ),
        {"id": event_id, "eid": new_id()},
    )
    await session.commit()
    return event_id


@pytest.mark.asyncio
async def test_insert_succeeds_and_schema_version_defaults_to_one(
    msession: AsyncSession,
) -> None:
    event_id = await _insert_event(msession)
    row = await msession.execute(
        text("SELECT schema_version FROM events WHERE id = :id"),
        {"id": event_id},
    )
    assert row.scalar_one() == 1


@pytest.mark.asyncio
async def test_update_is_rejected_by_trigger(msession: AsyncSession) -> None:
    event_id = await _insert_event(msession)
    # Триггер срабатывает на этапе выполнения statement (не commit). Ловим тут же.
    with pytest.raises(DBAPIError) as exc:
        await msession.execute(
            text("UPDATE events SET event_type = 'hacked' WHERE id = :id"),
            {"id": event_id},
        )
    assert "append-only" in str(exc.value).lower()
    await msession.rollback()


@pytest.mark.asyncio
async def test_delete_is_rejected_by_trigger(msession: AsyncSession) -> None:
    event_id = await _insert_event(msession)
    with pytest.raises(DBAPIError) as exc:
        await msession.execute(
            text("DELETE FROM events WHERE id = :id"),
            {"id": event_id},
        )
    assert "append-only" in str(exc.value).lower()
    await msession.rollback()


@pytest.mark.asyncio
async def test_revoke_present_in_catalog(msession: AsyncSession) -> None:
    """Рубеж 2: у novryn_app НЕТ привилегий UPDATE/DELETE на events."""
    role_exists = await msession.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = 'novryn_app'")
    )
    if role_exists.scalar_one_or_none() is None:
        pytest.skip("Роль novryn_app недоступна — REVOKE-проверка пропущена")

    has_update = await msession.execute(
        text("SELECT has_table_privilege('novryn_app', 'events', 'UPDATE')")
    )
    has_delete = await msession.execute(
        text("SELECT has_table_privilege('novryn_app', 'events', 'DELETE')")
    )
    assert has_update.scalar_one() is False
    assert has_delete.scalar_one() is False
    # INSERT/SELECT остаются разрешёнными.
    has_insert = await msession.execute(
        text("SELECT has_table_privilege('novryn_app', 'events', 'INSERT')")
    )
    assert has_insert.scalar_one() is True


@pytest.mark.asyncio
async def test_mutation_rejected_under_novryn_app_role(
    msession: AsyncSession,
) -> None:
    """Под SET ROLE novryn_app UPDATE/DELETE отвергаются (триггер + REVOKE)."""
    role_exists = await msession.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = 'novryn_app'")
    )
    if role_exists.scalar_one_or_none() is None:
        pytest.skip("Роль novryn_app недоступна — SET ROLE-проверка пропущена")

    event_id = await _insert_event(msession)
    await msession.execute(text("SET ROLE novryn_app"))
    with pytest.raises(DBAPIError):
        await msession.execute(
            text("UPDATE events SET event_type = 'x' WHERE id = :id"),
            {"id": event_id},
        )
    # Ошибка отравляет транзакцию; rollback сбрасывает её и SET ROLE.
    await msession.rollback()

    await msession.execute(text("SET ROLE novryn_app"))
    with pytest.raises(DBAPIError):
        await msession.execute(
            text("DELETE FROM events WHERE id = :id"),
            {"id": event_id},
        )
    await msession.rollback()
