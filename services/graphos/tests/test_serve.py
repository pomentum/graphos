"""``python -m graphos``: the settings it hands uvicorn, no server started."""

from __future__ import annotations

from typing import Any

import pytest

from graphos import __main__ as entry


def _served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
    entry.serve()
    return seen


def test_idle_connections_outlast_a_proxys_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPHOS_KEEP_ALIVE_S", raising=False)
    assert _served(monkeypatch)["timeout_keep_alive"] == 75


def test_the_keep_alive_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHOS_KEEP_ALIVE_S", "120")
    assert _served(monkeypatch)["timeout_keep_alive"] == 120
