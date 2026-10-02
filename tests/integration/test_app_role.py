"""Интеграция (Фаза 4, план 02): все операции сервисов работают под ролью приложения.

Wave-0 RED baseline. До миграции 004 у ``novryn_app`` были только SELECT/INSERT, а
сервисы делают UPDATE (правка задачи, soft-delete, завершение сессии, upsert памяти).
Значит, приложение могло работать лишь под владельцем таблиц — и тогда REVOKE на
журнале событий декоративен (Pitfall 5).

Соединения открываются СРАЗУ под ролью (startup-параметр ``role``): так роль
переживает откаты транзакций внутри сервисов, как и будет в эксплуатации, где сервер
входит LOGIN-ролью — членом ``novryn_app``.

Поведение, фиксируемое тестами:
- задачи, иерархия, зависимости, вложения, сессии, фокус, память, паттерны, insights,
  история и токены — всё выполняется под ``novryn_app``;
- физическое удаление доменных строк запрещено (только soft-delete);
- снимки фокуса и журнал событий нельзя править; держателя токена — тоже.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from novryn.domain.events import ActorType
from novryn.repositories import event_read, focus_read, insights_read, task_read
from novryn.repositories.token_read import find_principal
from novryn.services import (
    attachment_service,
    dependency_service,
    focus_service,
    hierarchy_service,
    memory_service,
    pattern_service,
    session_service,
    task_service,
    token_service,
)

_USER = ActorType.USER
_HERMES = ActorType.HERMES


@pytest_asyncio.fixture
async def app_sessionmaker(
    database_url: str,
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий, чьи соединения открыты под ролью novryn_app."""
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"role": "novryn_app"}},
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_connections_run_as_app_role(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with app_sessionmaker() as s:
        assert await s.scalar(text("SELECT current_user")) == "novryn_app"


@pytest.mark.asyncio
async def test_task_domain_under_app_role(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    maker = app_sessionmaker
    async with maker() as s:
        root = await task_service.create_task(
            s, title="корень", actor_type=_USER, actor_id=None, status="TODO"
        )
    async with maker() as s:
        await task_service.update_task(
            s, task_id=root, actor_type=_USER, actor_id=None, changes={"title": "корень 2"}
        )
    async with maker() as s:
        child, other = await task_service.create_subtasks(
            s,
            parent_task_id=root,
            subtasks=[{"title": "дочерняя"}, {"title": "соседняя"}],
            actor_type=_HERMES,
            actor_id=None,
        )
    async with maker() as s:
        await task_service.block(
            s, task_id=child, reason="ждёт", actor_type=_USER, actor_id=None
        )
    async with maker() as s:
        await task_service.unblock(s, task_id=child, actor_type=_USER, actor_id=None)
    async with maker() as s:
        await task_service.complete_task(s, task_id=child, actor_type=_USER, actor_id=None)
    async with maker() as s:
        await task_service.archive_task(s, task_id=other, actor_type=_USER, actor_id=None)

    async with maker() as s:
        await dependency_service.link_tasks(
            s, task_id=root, depends_on_task_id=child, actor_type=_USER, actor_id=None
        )
    async with maker() as s:
        assert await dependency_service.unlink_tasks(
            s, task_id=root, depends_on_task_id=child, actor_type=_USER, actor_id=None
        )

    async with maker() as s:
        attachment_id = await attachment_service.attach_resource(
            s,
            task_id=root,
            type="URL",
            url="https://example.org",
            actor_type=_USER,
            actor_id=None,
        )
    async with maker() as s:
        assert await attachment_service.detach_resource(
            s, attachment_id=attachment_id, actor_type=_USER, actor_id=None
        )

    async with maker() as s:
        session_id = await session_service.start_session(
            s, task_id=root, planned_minutes=25, actor_type=_USER, actor_id=None
        )
    async with maker() as s:
        await session_service.finish_session(
            s,
            session_id=session_id,
            actual_minutes=30,
            result="COMPLETED",
            actor_type=_USER,
            actor_id=None,
        )

    async with maker() as s:
        tree = await hierarchy_service.get_task_tree(s, root)
        assert len(tree["children"]) == 2
        found = await task_read.search_tasks(s, q="корень")
        assert root in [t.id for t in found]
        history = await event_read.get_entity_history(s, entity_type="task", entity_id=root)
        assert len(history) == 2  # created + updated
        assert (await insights_read.task_insights(s, root))["task_id"] == str(root)


@pytest.mark.asyncio
async def test_intelligence_domain_under_app_role(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    maker = app_sessionmaker
    day = datetime.date(2026, 7, 2)
    async with maker() as s:
        task_id = await task_service.create_task(
            s, title="фокус", actor_type=_USER, actor_id=None
        )
    async with maker() as s:
        await focus_service.generate_daily_focus(
            s,
            date=day,
            items=[{"task_id": task_id, "rank": 1, "reason": None}],
            actor_type=_HERMES,
            actor_id=None,
        )
    async with maker() as s:
        assert [r.task_id for r in await focus_read.get_today_tasks(s, day)] == [task_id]

    for content in ("первое", "второе"):  # create, затем in-place update
        async with maker() as s:
            await memory_service.memory_store(
                s,
                memory_type="app-role-memory",
                content=content,
                confidence=0.5,
                source="test",
                actor_type=_HERMES,
                actor_id=None,
            )
    for confidence in (0.4, 0.6):  # create, затем in-place update
        async with maker() as s:
            await pattern_service.pattern_store(
                s,
                pattern_type="app-role-pattern",
                confidence=confidence,
                evidence_json={},
                actor_type=_HERMES,
                actor_id=None,
            )
    async with maker() as s:
        assert await insights_read.user_insights(s)


@pytest.mark.asyncio
async def test_tokens_under_app_role(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    maker = app_sessionmaker
    async with maker() as s:
        issued = await token_service.issue_token(
            s,
            name="app-role",
            holder_type=_HERMES,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
    async with maker() as s:
        assert await find_principal(s, issued.token) is not None
    async with maker() as s:
        assert await token_service.revoke_token(
            s, token_id=issued.id, actor_type=ActorType.SYSTEM, actor_id=None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM tasks",
        "DELETE FROM attachments",
        "UPDATE daily_focus SET rank = rank",
        "UPDATE events SET event_type = event_type",
        "UPDATE api_tokens SET holder_type = 'USER'",
        "UPDATE api_tokens SET token_hash = token_hash",
    ],
)
async def test_forbidden_statements_rejected(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    statement: str,
) -> None:
    async with app_sessionmaker() as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text(statement))
