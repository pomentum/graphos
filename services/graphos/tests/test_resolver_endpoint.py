"""Endpoint-level tests for ``POST /v1/{tenant}/graph/edges:resolve``.

Pure-Python — registers a tenant via the in-process registry, then
asserts the endpoint's request validation, missing-rule rejection, and
empty-catalogue zero-emit semantics.  DB-touching paths (actual edge
INSERTs) live in the integration suite, gated on ``GRAPHOS_TEST_DSN``.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.bootstrap import TenantSpec, remember
from graphos.config import Settings
from graphos.resolver.catalogue import (
    forget_catalogue_for_test,
    install_catalogue_for_test,
)


def _client() -> TestClient:
    settings = Settings(db_password="placeholder", auth_tokens_file=None, auth_disabled=True)
    app = create_app(settings=settings)
    # Inject a stub pool so the endpoint's ``request.app.state.pool``
    # access doesn't 500 in a DB-less unit test.  The catalogue refresh
    # is patched out in the relevant cases below.
    app.state.pool = object()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def registered_tenant() -> str:
    """Plug a fake tenant straight into the in-process registry."""
    name = "tenant_acme"
    remember(TenantSpec(
        name=name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES", "CITES"),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "name",
                "require_different": ["team"],
            }),
            # CITES deliberately has NO rule registered to exercise the
            # "missing rule" reject path.
        ),
    ))
    # Catalogue empty — no DB; the endpoint must short-circuit to
    # zero-emit with a warning rather than 500.
    forget_catalogue_for_test(name)
    install_catalogue_for_test(name, {})
    yield name
    # Cleanup: drop the registration so other tests don't see it.
    from graphos.bootstrap import _REGISTERED
    _REGISTERED.pop(name, None)
    forget_catalogue_for_test(name)


def test_resolve_unknown_tenant_404() -> None:
    client = _client()
    resp = client.post(
        "/v1/totally_unknown/graph/edges:resolve",
        json={"edge_types": ["REFERENCES"]},
    )
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert "not registered" in resp.json()["detail"]


def test_resolve_missing_rule_400(registered_tenant: str) -> None:
    """Requesting an edge_type that has no registered ``resolve_rule``
    must reject — silent zero would mislead the caller."""
    client = _client()
    resp = client.post(
        f"/v1/{registered_tenant}/graph/edges:resolve",
        json={"edge_types": ["CITES"]},  # registered but no rule
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "no resolve_rule" in resp.json()["detail"]
    assert "CITES" in resp.json()["detail"]


def test_resolve_rejects_unsafe_edge_type_name(registered_tenant: str) -> None:
    client = _client()
    resp = client.post(
        f"/v1/{registered_tenant}/graph/edges:resolve",
        json={"edge_types": ["FOO; DROP TABLE x;"]},
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_resolve_empty_catalogue_zero_emit_with_warning(
    registered_tenant: str,
) -> None:
    """When no vertex label carries the rule's referenced properties,
    the endpoint returns ``edges_written=0`` with a warning rather than
    failing the call (per design doc §"Open questions" #3)."""
    client = _client()
    # The endpoint looks the catalogue up on entry by hitting the DB —
    # which is unavailable here.  We patch catalogue_for_properties to return
    # the seeded empty catalogue instead of hitting the DB.
    from graphos.api import graph as graph_mod
    from graphos.resolver import catalogue as cat_mod
    original = graph_mod.catalogue_for_properties

    async def _fake_refresh(pool, tenant, properties):  # noqa: ANN001 — test stub
        return cat_mod._TEST_CATALOGUES[tenant]

    from graphos.api import graph as graph_mod
    graph_mod.catalogue_for_properties = _fake_refresh
    try:
        resp = client.post(
            f"/v1/{registered_tenant}/graph/edges:resolve",
            json={},  # resolve all registered types — just REFERENCES
        )
    finally:
        graph_mod.catalogue_for_properties = original

    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["tenant"] == registered_tenant
    assert body["total_edges_written"] == 0
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["edge_type"] == "REFERENCES"
    assert result["edges_written"] == 0
    assert result["warning"] is not None
    assert "properties" in result["warning"]


def test_resolve_empty_request_resolves_all_registered(
    registered_tenant: str,
) -> None:
    """Omitting ``edge_types`` resolves every registered rule (here, just REFERENCES)."""
    client = _client()
    from graphos.api import graph as graph_mod
    from graphos.resolver import catalogue as cat_mod
    original = graph_mod.catalogue_for_properties

    async def _fake_refresh(pool, tenant, properties):  # noqa: ANN001
        return cat_mod._TEST_CATALOGUES[tenant]

    from graphos.api import graph as graph_mod
    graph_mod.catalogue_for_properties = _fake_refresh
    try:
        resp = client.post(
            f"/v1/{registered_tenant}/graph/edges:resolve",
            json={},
        )
    finally:
        graph_mod.catalogue_for_properties = original
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    types = [r["edge_type"] for r in body["results"]]
    assert types == ["REFERENCES"]  # CITES has no rule


def test_register_tenant_accepts_resolve_rule_shape() -> None:
    """The TenantSpecBody round-trips the new ``resolve_rule`` shape
    end-to-end (validation path)."""
    client = _client()
    # No DB available, so the actual schema bootstrap will 500 — but
    # the body-level validation runs first.  We only assert here that
    # the body shape is *accepted* (no 422); the 500 from the DB-less
    # bootstrap is expected and uninteresting.
    resp = client.post(
        "/v1/tenants",
        json={
            "name": "validator_check",
            "vertex_labels": ["Document"],
            "edge_labels": [
                {
                    "name": "REFERENCES",
                    "resolve_rule": {
                        "src_has_array":     "tag_keys",
                        "dst_value_from":    "name",
                        "require_different": ["team"],
                    },
                },
            ],
        },
    )
    # 422 would mean the contract rejected the body.  We accept either
    # a successful bootstrap (with a real DB) or a 5xx (no DB) — but
    # NOT a 422.
    assert resp.status_code != HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.fixture
def registered_multi_rule_tenant() -> str:
    """Tenant with TWO rules for the same edge_type — exercises the
    multi-rule-per-edge-type code path that lets a single edge_type
    aggregate matches from independent src-side property arrays."""
    name = "tenant_multi_rule"
    remember(TenantSpec(
        name=name,
        vertex_labels=("Document",),
        edge_labels=("RELATES_TO",),
        resolve_rules=(
            ("RELATES_TO", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "key",
                "require_different": ["team"],
            }),
            ("RELATES_TO", {
                "src_has_array":     "related_tag_keys",
                "dst_value_from":    "key",
                "require_different": ["team"],
            }),
        ),
    ))
    forget_catalogue_for_test(name)
    install_catalogue_for_test(name, {})
    yield name
    from graphos.bootstrap import _REGISTERED
    _REGISTERED.pop(name, None)
    forget_catalogue_for_test(name)


def test_resolve_multi_rule_per_edge_type_returns_single_result(
    registered_multi_rule_tenant: str,
) -> None:
    """Two rules under the same edge_type aggregate into ONE result
    entry (per-edge-type counts), not two — the response shape stays
    one row per edge_type even with multiple rules behind it."""
    client = _client()
    from graphos.api import graph as graph_mod
    from graphos.resolver import catalogue as cat_mod
    original = graph_mod.catalogue_for_properties

    async def _fake_refresh(pool, tenant, properties):  # noqa: ANN001
        return cat_mod._TEST_CATALOGUES[tenant]

    from graphos.api import graph as graph_mod
    graph_mod.catalogue_for_properties = _fake_refresh
    try:
        resp = client.post(
            f"/v1/{registered_multi_rule_tenant}/graph/edges:resolve",
            json={},
        )
    finally:
        graph_mod.catalogue_for_properties = original
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    # Both rules are for RELATES_TO — the result list has exactly one
    # entry for that edge_type, not two.
    assert len(body["results"]) == 1
    assert body["results"][0]["edge_type"] == "RELATES_TO"
    # Empty catalogue → all rules report "no label", aggregate stays 0.
    assert body["results"][0]["edges_written"] == 0


async def _e2e_indexer_with_live_db(db_pool) -> None:  # noqa: ANN001
    """Helper for the requires_db E2E test below: register a tenant with
    a resolve_rule and assert the matching pg_indexes rows exist."""
    from graphos.bootstrap import (
        bootstrap_tenant,
        persist_spec,
        remember,
        unregister_tenant,
    )
    from graphos.resolver.catalogue import forget_catalogue_for_test
    from graphos.resolver.indexer import ensure_resolve_rule_indexes

    name = "tenant_indexer_e2e"
    spec = TenantSpec(
        name=name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES",),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "name",
                "require_different": ["team"],
            }),
        ),
    )
    try:
        await bootstrap_tenant(db_pool, spec)
        await persist_spec(db_pool, spec)
        remember(spec)
        # Seed at least one node so the catalogue picks the label up.
        async with db_pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO \"{name}\".\"Document\" (properties) "
                "VALUES ('{\"tag_keys\": [\"x\"], \"name\": \"n\", "
                "\"team\": \"r\"}'::agtype);"
            )
        ensured = await ensure_resolve_rule_indexes(
            db_pool, tenant=name, spec=spec,
        )
        assert ensured, "expected indexer to ensure at least one index"
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = $1 AND tablename = 'Document';",
                name,
            )
        names = {r["indexname"] for r in rows}
        # Each rule property should land an index.
        assert "idx_Document_tag_keys_gin" in names
        assert "idx_Document_name_btree" in names
        assert "idx_Document_team_btree" in names
    finally:
        forget_catalogue_for_test(name)
        await unregister_tenant(db_pool, name)


from .conftest import requires_db  # noqa: E402 — keep DB-less tests above


@requires_db
@pytest.mark.asyncio
async def test_resolve_rule_registration_creates_pg_indexes(db_pool) -> None:  # noqa: ANN001
    """End-to-end: registering a tenant with a resolve_rule creates the
    matching functional indexes on the candidate vertex tables."""
    await _e2e_indexer_with_live_db(db_pool)


def test_register_response_exposes_n_resolver_indexes() -> None:
    """The registration response surfaces the count of auto-derived
    functional indexes graphos ensured for the tenant's resolve rules.

    DB-less: bootstrap will fail, but we hit the JSON-schema response
    contract directly to confirm the field is part of the wire shape.
    """
    from graphos.contracts.tenants import RegisterResponse
    r = RegisterResponse(
        tenant="t",
        vertex_labels=["Document"],
        edge_labels=["REFERENCES"],
        n_indexes=0,
        n_generated_columns=0,
        n_inheritance_edges=0,
        n_resolve_rules=1,
        n_resolver_indexes=4,
    )
    assert r.n_resolver_indexes == 4
    payload = r.model_dump()
    assert "n_resolver_indexes" in payload
    assert payload["n_resolver_indexes"] == 4


def test_register_tenant_rejects_unknown_resolve_rule_key() -> None:
    """The closed DSL must reject unknown keys at registration."""
    client = _client()
    resp = client.post(
        "/v1/tenants",
        json={
            "name": "validator_check",
            "edge_labels": [
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":  "tag_keys",
                        "dst_value_from": "name",
                        "unknown_key":    "boom",
                    },
                },
            ],
        },
    )
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    detail = resp.json()["detail"]
    # Find the message somewhere in the validation detail.
    flat = str(detail)
    assert "unknown_key" in flat
