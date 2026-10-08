"""Security-relevant behaviour: DDL validation, timeouts, fail-closed auth, audit identity."""

from __future__ import annotations

import logging
from http import HTTPStatus

import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.contracts.tenants import TenantSpecBody
from graphos.identity import Principal

_BASE = {"name": "demo", "vertex_labels": ["Doc"], "edge_labels": ["LINKS"]}


# ── registration DDL ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ddl",
    [
        'CREATE INDEX IF NOT EXISTS doc_name ON "demo"."Doc" ((properties::jsonb->>\'name\'));',
        'CREATE UNIQUE INDEX IF NOT EXISTS "u" ON demo."Doc" USING gin '
        "((properties::jsonb ->> 'name') gin_trgm_ops)",
        'CREATE INDEX IF NOT EXISTS e ON "demo"."LINKS" (end_community_id) '
        "WHERE end_community_id IS NOT NULL",
    ],
)
def test_index_ddl_accepted(ddl: str) -> None:
    body = TenantSpecBody(**_BASE, indexes=[ddl])
    assert not body.indexes[0].endswith(";")


@pytest.mark.parametrize(
    "ddl",
    [
        'DROP TABLE "demo"."Doc"',
        'CREATE INDEX IF NOT EXISTS x ON "demo"."Doc" (a); DROP TABLE t',
        'CREATE INDEX x ON "demo"."Doc" (a)',                       # no IF NOT EXISTS
        'CREATE INDEX IF NOT EXISTS x ON "other"."Doc" (a)',        # another tenant
        'CREATE INDEX IF NOT EXISTS x ON "demo"."Unregistered" (a)',
        'CREATE INDEX IF NOT EXISTS x ON "demo"."Doc" (a) -- comment',
        'CREATE INDEX IF NOT EXISTS x ON "demo"."Doc" ((f($$a$$)))',
        'CREATE INDEX IF NOT EXISTS x ON "demo"."Doc" ((a)',        # unbalanced
    ],
)
def test_index_ddl_rejected(ddl: str) -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(**_BASE, indexes=[ddl])


def test_generated_column_ddl() -> None:
    ok = (
        'ALTER TABLE "demo"."Doc" ADD COLUMN IF NOT EXISTS score double precision '
        "GENERATED ALWAYS AS (((properties::jsonb)->>'s')::float8) STORED"
    )
    assert TenantSpecBody(**_BASE, generated_columns=[ok]).generated_columns == [ok]
    for bad in (
        # A second ALTER TABLE subcommand chained with a comma.
        'ALTER TABLE "demo"."Doc" ADD COLUMN c text GENERATED ALWAYS AS (1) STORED, '
        "DROP COLUMN properties",
        # The same, hiding a ')' inside a string literal.
        "ALTER TABLE \"demo\".\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS ('x)') STORED, "
        "DROP COLUMN properties",
        'ALTER TABLE "demo"."Doc" DROP COLUMN properties',
    ):
        with pytest.raises(ValueError):
            TenantSpecBody(**_BASE, generated_columns=[bad])


@pytest.mark.parametrize(
    "column", ["graphos_rev", "GRAPHOS_X", '"graphos_rev"', "end_community_id"],
)
def test_generated_column_cannot_take_a_graphos_column_name(column: str) -> None:
    ddl = (
        f'ALTER TABLE "demo"."Doc" ADD COLUMN {column} bigint '
        "GENERATED ALWAYS AS (1) STORED"
    )
    with pytest.raises(ValueError, match="reserved"):
        TenantSpecBody(**_BASE, generated_columns=[ddl])


# ── statement timeout → 504 ────────────────────────────────────────────────


def test_statement_timeout_maps_to_504_problem_json() -> None:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))

    @app.get("/boom")
    async def boom() -> None:
        raise asyncpg.exceptions.QueryCanceledError("canceling statement due to statement timeout")

    resp = TestClient(app).get("/boom")
    assert resp.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["status"] == HTTPStatus.GATEWAY_TIMEOUT


# ── auth fails closed ──────────────────────────────────────────────────────


def test_requests_rejected_without_tokens_or_explicit_opt_out() -> None:
    app = create_app(Settings(db_password="placeholder"))
    resp = TestClient(app).get("/v1/tenants")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


# ── audit names the real consumer ──────────────────────────────────────────


def test_audit_records_the_consumer(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(Settings(db_password="placeholder"))
    app.state.authenticator = StaticAuthenticator({"tok_ops": Principal.static("ops", admin=True)})
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        client.post("/v1/tenants", headers={"Authorization": "Bearer tok_ops"}, json={"name": "t1"})
    (record,) = [r for r in caplog.records if r.name == "graphos.audit"]
    assert record.consumer == "ops"
