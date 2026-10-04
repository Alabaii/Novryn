"""Юнит-тесты конфигурации (без БД): часовой пояс пользователя (Фаза 4, план 03).

Wave-0 RED baseline: ``novryn.config.get_timezone`` ещё не существует.

«Сегодня» для фокуса дня считается в часовом поясе пользователя ``NOVRYN_TIMEZONE``:
контейнеры Docker живут в UTC, и без настройки после полуночи по местному времени
было бы ещё «вчера».
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from novryn.config import get_timezone


def test_timezone_defaults_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVRYN_TIMEZONE", raising=False)
    assert get_timezone() == ZoneInfo("UTC")


def test_timezone_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVRYN_TIMEZONE", "Europe/Moscow")
    assert get_timezone() == ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "../etc/passwd"])
def test_unknown_timezone_is_rejected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("NOVRYN_TIMEZONE", name)
    with pytest.raises(ValueError, match="NOVRYN_TIMEZONE"):
        get_timezone()


def test_page_size_fits_repository_ceilings() -> None:
    """has_more считается запросом limit+1 — он не должен упираться в потолок репозитория."""
    from novryn.mcp.params import MAX_LIMIT
    from novryn.repositories import event_read, memory_read, task_read

    for ceiling in (
        task_read._SEARCH_LIMIT_CEILING,
        memory_read._SEARCH_LIMIT_CEILING,
        event_read._HISTORY_LIMIT_CEILING,
    ):
        assert MAX_LIMIT + 1 <= ceiling
