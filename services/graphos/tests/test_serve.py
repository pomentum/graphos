"""``python -m graphos``: the settings it hands uvicorn, no server started."""

from __future__ import annotations

import pytest
import uvicorn

from graphos import __main__ as entry


def _served(monkeypatch: pytest.MonkeyPatch) -> uvicorn.Config:
    seen: dict[str, uvicorn.Config] = {}

    def run(self: entry.GraphosServer, sockets: object = None) -> None:
        seen["config"] = self.config
        self.started = True

    monkeypatch.setattr(entry.GraphosServer, "run", run)
    with pytest.raises(SystemExit) as exited:
        entry.serve()
    assert exited.value.code == 0
    return seen["config"]


def test_idle_connections_outlast_a_proxys_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPHOS_KEEP_ALIVE_S", raising=False)
    assert _served(monkeypatch).timeout_keep_alive == 75


def test_the_keep_alive_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHOS_KEEP_ALIVE_S", "120")
    assert _served(monkeypatch).timeout_keep_alive == 120


def test_a_stop_not_made_by_the_drain_is_bounded_by_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHOS_SHUTDOWN_GRACE_S", "30")
    assert _served(monkeypatch).timeout_graceful_shutdown == 30


def test_a_server_that_never_started_exits_with_uvicorns_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entry.GraphosServer, "run", lambda self, sockets=None: None)
    with pytest.raises(SystemExit) as exited:
        entry.serve()
    assert exited.value.code == 3
