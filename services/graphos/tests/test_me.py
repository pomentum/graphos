"""``GET /v1/me`` and ``GET /v1/{tenant}/schema``: what a non-admin client may learn.

Pure Python: tenants are put in the in-memory registry, no database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import TokenStore
from graphos.bootstrap import TenantSpec, forget, remember
from graphos.config import Settings

TOKENS = {
    "tok_read": {"consumer": "reader", "tenants": ["acme", "unregistered"], "access": "read"},
    "tok_write": {"consumer": "writer", "tenants": ["acme"], "access": "write"},
    "tok_admin": {"consumer": "ops", "tenants": [], "admin": True},
}
ACME = TenantSpec(
    name="acme",
    vertex_labels=("Person", "Employee", "Doc"),
    edge_labels=("WROTE", "SAME_TEAM"),
    indexes=('CREATE INDEX IF NOT EXISTS "Doc_x" ON acme."Doc" (id)',),
    label_hierarchy=(("Employee", "Person"),),
    resolve_rules=(("SAME_TEAM", {"src_label": "Person", "dst_label": "Person"}),),
    meta={"owner": "team-a"},
    search_indexes={"names": {"labels": ["*"], "properties": ["name"],
                              "analyzer": "identifier", "language": "simple"}},
)
OTHER = TenantSpec(name="other", vertex_labels=("Thing",))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(TOKENS))
    app = create_app(Settings(db_password="placeholder", auth_tokens_file=tokens))
    app.state.token_store = TokenStore(tokens)
    remember(ACME)
    remember(OTHER)
    try:
        yield TestClient(app)
    finally:
        forget("acme")
        forget("other")


def _get(client: TestClient, path: str, token: str):  # noqa: ANN202
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_me_lists_the_registered_tenants_a_token_may_use(client: TestClient) -> None:
    body = _get(client, "/v1/me", "tok_read").json()
    assert body == {"consumer": "reader", "admin": False, "access": "read", "tenants": ["acme"]}
    assert _get(client, "/v1/me", "tok_write").json()["access"] == "write"
    admin = _get(client, "/v1/me", "tok_admin").json()
    assert admin["admin"] is True
    assert {"acme", "other"} <= set(admin["tenants"])
    assert client.get("/v1/me").status_code == HTTPStatus.UNAUTHORIZED


def test_schema_gives_labels_and_search_indexes_but_not_the_admin_spec(
    client: TestClient,
) -> None:
    resp = _get(client, "/v1/acme/schema", "tok_read")
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["vertex_labels"] == ["Person", "Employee", "Doc"]
    assert body["edge_labels"] == ["WROTE", "SAME_TEAM"]
    assert body["label_hierarchy"] == {"Employee": "Person"}
    assert body["search_indexes"]["names"]["analyzer"] == "identifier"
    assert body["rule_edge_labels"] == ["SAME_TEAM"]
    # DDL, rule bodies and metadata stay with the admin-only tenant endpoints.
    assert not {"indexes", "generated_columns", "meta", "resolve_rules"} & set(body)


def test_schema_needs_the_tenant_and_a_registration(client: TestClient) -> None:
    assert _get(client, "/v1/other/schema", "tok_read").status_code == HTTPStatus.FORBIDDEN
    assert _get(client, "/v1/other/schema", "tok_admin").status_code == HTTPStatus.OK
    assert _get(client, "/v1/unregistered/schema", "tok_read").status_code == HTTPStatus.NOT_FOUND
    assert client.get("/v1/acme/schema").status_code == HTTPStatus.UNAUTHORIZED
