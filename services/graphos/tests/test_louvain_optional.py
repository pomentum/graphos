"""Louvain is an optional extra: graphos must import and answer cleanly without it."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from graphos.api import louvain as louvain_api
from graphos.app import create_app
from graphos.config import Settings


def _client() -> TestClient:
    settings = Settings(db_password="placeholder", auth_tokens_file=None, auth_disabled=True)
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def test_recompute_returns_501_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(louvain_api, "LOUVAIN_AVAILABLE", False)
    resp = _client().post("/v1/acme/recompute-communities", json={})
    assert resp.status_code == HTTPStatus.NOT_IMPLEMENTED
    assert "graphos[louvain]" in resp.text
