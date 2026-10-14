"""``GET /v1/me`` and ``GET /v1/{tenant}/schema``: what a non-admin client may learn.

Pure Python: tenants are put in the in-memory registry, no database.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import TenantSpec, forget, remember
from graphos.config import Settings
from graphos.identity import Principal

PRINCIPALS = {
    "tok_read": Principal.static("reader", tenants={"acme": "read", "unregistered": "read"}),
    "tok_write": Principal.static("writer", tenants={"acme": "write"}),
    "tok_svc": Principal.static("svc", tenants={"oth*": "maintain"}, create_tenants=["sol_*"]),
    "tok_admin": Principal.static("ops", admin=True),
}
ACME = TenantSpec(
    name="acme",
    vertex_labels=("Person", "Employee", "Doc"),
    edge_labels=("WROTE", "SAME_TEAM"),
    property_indexes=({"label": "Doc", "properties": ["x"], "kind": "equality",
                       "unique": False, "name": None},),
    label_hierarchy=(("Employee", "Person"),),
    resolve_rules=(("SAME_TEAM", {"src_label": "Person", "dst_label": "Person"}),),
    meta={"owner": "team-a"},
    search_indexes={"names": {"labels": ["*"], "properties": ["name"],
                              "analyzer": "identifier", "language": "simple"}},
)
OTHER = TenantSpec(name="other", vertex_labels=("Thing",))


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(db_password="placeholder"))
    app.state.authenticator = StaticAuthenticator(PRINCIPALS)
    remember(ACME)
    remember(OTHER)
    try:
        yield TestClient(app)
    finally:
        forget("acme")
        forget("other")


def _get(client: TestClient, path: str, token: str):  # noqa: ANN202
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_me_lists_the_registered_tenants_and_levels(client: TestClient) -> None:
    body = _get(client, "/v1/me", "tok_read").json()
    assert body == {
        "consumer": "reader", "kind": "person", "via": "password", "admin": False,
        "must_change": False,
        "tenants": ["acme"], "levels": {"acme": "read"}, "create_tenants": [], "preferences": {},
    }
    assert _get(client, "/v1/me", "tok_write").json()["levels"] == {"acme": "write"}
    svc = _get(client, "/v1/me", "tok_svc").json()
    assert (svc["tenants"], svc["levels"], svc["create_tenants"]) == (
        ["other"], {"other": "maintain"}, ["sol_*"],
    )
    admin = _get(client, "/v1/me", "tok_admin").json()
    assert admin["admin"] is True and admin["create_tenants"] == ["*"]
    assert {"acme", "other"} <= set(admin["tenants"])
    assert admin["levels"]["acme"] == "owner"
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
    assert not {"property_indexes", "generated_columns", "meta", "resolve_rules"} & set(body)


def test_schema_needs_the_tenant_and_a_registration(client: TestClient) -> None:
    assert _get(client, "/v1/other/schema", "tok_read").status_code == HTTPStatus.FORBIDDEN
    assert _get(client, "/v1/other/schema", "tok_admin").status_code == HTTPStatus.OK
    assert _get(client, "/v1/unregistered/schema", "tok_read").status_code == HTTPStatus.NOT_FOUND
    assert client.get("/v1/acme/schema").status_code == HTTPStatus.UNAUTHORIZED
