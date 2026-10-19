"""A token an MCP client got only reads (ADR-0008), without a database.

The principal is held to reading where it is made, so every level check
refuses what writes; the account changes a route declares as reads are
refused by name.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import READ_ONLY_REFUSED, StaticAuthenticator, reading_only
from graphos.config import Settings
from graphos.identity import Level, Principal

PERSON = Principal(
    consumer="rita", subject="user:rita", levels=(("acme", Level.OWNER),),
    create_tenants=("acme*",),
)


def test_a_read_only_principal_reads_what_it_could_and_nothing_more() -> None:
    held = reading_only(PERSON)
    assert held.read_only and held.level("acme") is Level.READ
    assert not held.can_create("acme2")
    admin = reading_only(Principal(consumer="ops", subject="user:ops", admin=True))
    assert not admin.admin and admin.level("anything") is Level.READ


def test_every_write_and_account_change_is_refused_to_a_read_only_token() -> None:
    app = create_app(Settings(db_password="x"))
    app.state.authenticator = StaticAuthenticator({
        "ro": reading_only(PERSON), "rw": PERSON,
    })
    client = TestClient(app, raise_server_exceptions=False)
    refused = [
        ("POST", "/v1/acme/graph/nodes", {"nodes": []}),
        ("DELETE", "/v1/acme/graph/nodes/x", None),
        ("POST", "/v1/tenants", {"name": "acme2", "vertex_labels": ["Doc"]}),
        ("POST", "/v1/me/password", {"current": "a", "new": "b"}),
        ("PATCH", "/v1/me/preferences", {"theme": "dark"}),
        ("POST", "/v1/auth/mcp/approve", {}),
    ]
    assert {path for _, path, _ in refused} >= READ_ONLY_REFUSED
    for method, path, body in refused:
        r = client.request(method, path, json=body, headers={"Authorization": "Bearer ro"})
        assert r.status_code == HTTPStatus.FORBIDDEN, (method, path, r.text)
    # The same person with their own sign-in is not refused by this check.
    r = client.request("PATCH", "/v1/me/preferences", json={"theme": "dark"},
                       headers={"Authorization": "Bearer rw"})
    assert r.status_code != HTTPStatus.FORBIDDEN
