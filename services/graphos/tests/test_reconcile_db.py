"""DB tests: edge reconciliation by difference (ADR-0001).

DB-gated.  A small tenant: services that depend on services and call
endpoints, plus documents tagged by a rule.  The API is driven in-process
through httpx's ASGI transport with the test pool.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import changes, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, forget, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_reconcile"
SERVICES = ["billing", "ledger", "auth", "mail", "search"]


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
        await conn.execute("DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", TENANT)
    forget(TENANT)


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Service", "Endpoint", "Doc", "Tag"),
        edge_labels=("DEPENDS_ON", "CALLS", "TAGGED"),
        resolve_rules=(("TAGGED", {"src_has_array": "tags", "dst_value_from": "name"}),),
    ))
    await writes.upsert_nodes(pool, tenant=TENANT, label="Service",
                              rows=[{"id": s, "props": {}} for s in SERVICES])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Endpoint",
                              rows=[{"id": "billing", "props": {}}, {"id": "e1", "props": {}}])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Service", dst_label="Service", edge_type="DEPENDS_ON",
        rows=[{"src_id": "billing", "dst_id": d, "props": {"w": 1}} for d in ("ledger", "auth")]
        + [{"src_id": "mail", "dst_id": "auth", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Service", dst_label="Endpoint", edge_type="DEPENDS_ON",
        rows=[{"src_id": "billing", "dst_id": "e1", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Service", dst_label="Service", edge_type="CALLS",
        rows=[{"src_id": "billing", "dst_id": "ledger", "props": {}}],
    )


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    await _seed(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    ) as client:
        yield client
    await _fresh(db_pool)


async def _edges(pool: asyncpg.Pool, edge_type: str) -> dict[tuple[str, str], tuple[int, Any]]:
    rows = await pool.fetch(
        "SELECT s.properties::text::jsonb ->> 'id' AS s, "
        "d.properties::text::jsonb ->> 'id' AS d, "
        "e.graphos_rev AS rev, e.properties::text AS props "
        f'FROM "{TENANT}"."{edge_type}" e '
        f'JOIN "{TENANT}"."_ag_label_vertex" s ON s.id = e.start_id '
        f'JOIN "{TENANT}"."_ag_label_vertex" d ON d.id = e.end_id;',
    )
    return {(r["s"], r["d"]): (r["rev"], json.loads(r["props"])) for r in rows}


def _node_body(edges: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"scope": "node", "type": "DEPENDS_ON", "node": {"label": "Service", "id": "billing"},
            "other_label": "Service", "edges": edges, **extra}


@requires_db
@pytest.mark.asyncio
async def test_a_node_scope_adds_changes_keeps_and_removes_only_inside_it(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    before = await _edges(db_pool, "DEPENDS_ON")
    r = await api.post("/edges:reconcile", json=_node_body([
        {"id": "ledger"},                          # kept as it is
        {"id": "mail", "props": {"w": 2}},         # added
        {"id": "nowhere"},                         # its end does not exist
    ]))
    assert r.status_code == HTTPStatus.OK, r.text
    body = r.json()
    assert (body["added"], body["changed"], body["kept"], body["removed"], body["skipped"]) == (
        1, 0, 1, 1, 1,
    )
    assert body["skipped_edges"] == [["billing", "nowhere"]]
    after = await _edges(db_pool, "DEPENDS_ON")
    assert ("billing", "auth") not in after                       # removed
    assert after[("billing", "ledger")] == before[("billing", "ledger")]   # not written
    assert after[("billing", "mail")][1] == {"w": 2}
    assert ("mail", "auth") in after                               # another node's edge
    assert ("billing", "e1") in after                              # another label's edge
    assert len(await _edges(db_pool, "CALLS")) == 1                # another type


@requires_db
@pytest.mark.asyncio
async def test_merge_and_replace_rewrite_only_edges_whose_properties_change(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    same = await api.post("/edges:reconcile", json=_node_body(
        [{"id": "ledger", "props": {"w": 1}}, {"id": "auth", "props": {"w": 1}}],
    ))
    assert (same.json()["changed"], same.json()["kept"]) == (0, 2)
    merged = await api.post("/edges:reconcile", json=_node_body(
        [{"id": "ledger", "props": {"x": 9}}, {"id": "auth"}],
    ))
    assert merged.json()["changed"] == 1
    rev, props = (await _edges(db_pool, "DEPENDS_ON"))[("billing", "ledger")]
    assert props == {"w": 1, "x": 9} and rev == 2  # noqa: PLR2004
    replaced = await api.post("/edges:reconcile", json=_node_body(
        [{"id": "ledger", "props": {"y": 1}}, {"id": "auth"}], props="replace",
    ))
    assert replaced.json()["changed"] == 1
    assert (await _edges(db_pool, "DEPENDS_ON"))[("billing", "ledger")][1] == {"y": 1}


@requires_db
@pytest.mark.asyncio
async def test_the_incoming_direction_and_a_dry_run(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    before = await _edges(db_pool, "DEPENDS_ON")
    body = {**_node_body([{"id": "billing"}]), "node": {"label": "Service", "id": "auth"},
            "direction": "in"}
    dry = await api.post("/edges:reconcile?dry_run=true", json=body)
    assert dry.status_code == HTTPStatus.OK, dry.text
    assert (dry.json()["kept"], dry.json()["removed"], dry.json()["dry_run"]) == (1, 1, True)
    assert await _edges(db_pool, "DEPENDS_ON") == before
    applied = await api.post("/edges:reconcile", json=body)
    assert applied.json()["removed"] == 1
    assert ("mail", "auth") not in await _edges(db_pool, "DEPENDS_ON")


@requires_db
@pytest.mark.asyncio
async def test_a_type_scope_from_ndjson_equals_a_fresh_load_and_is_recorded(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        start = await changes.head(conn)
    wanted = [("ledger", "auth"), ("search", "mail"), ("billing", "ledger")]
    lines = [{"scope": "type", "type": "DEPENDS_ON", "src_label": "Service",
              "dst_label": "Service"}]
    lines += [{"src_id": s, "dst_id": d} for s, d in wanted]
    r = await api.post(
        "/edges:reconcile", content="\n".join(json.dumps(x) for x in lines) + "\n",
        headers={"content-type": "application/x-ndjson"},
    )
    assert r.status_code == HTTPStatus.OK, r.text
    assert (r.json()["added"], r.json()["kept"], r.json()["removed"]) == (2, 1, 2)
    stored = await _edges(db_pool, "DEPENDS_ON")
    assert {p for p in stored if p != ("billing", "e1")} == set(wanted)
    assert ("billing", "e1") in stored          # Service -> Endpoint is outside the scope
    async with db_pool.acquire() as conn:
        page = await changes.read(conn, TENANT, start, 100)
    recorded = {(c.op, (c.detail or {}).get("reconcile"), c.count) for c in page.changes}
    assert ("upsert", "type", 2) in recorded and ("delete", "type", 2) in recorded


@requires_db
@pytest.mark.asyncio
async def test_bad_requests_are_refused_before_anything_is_written(
    api: httpx.AsyncClient,
) -> None:
    cases = [
        ({"scope": "node", "type": "DEPENDS_ON", "edges": []}, HTTPStatus.UNPROCESSABLE_ENTITY),
        (_node_body([], type="LINKS_TO"), HTTPStatus.BAD_REQUEST),
        (_node_body([], other_label="Person"), HTTPStatus.BAD_REQUEST),
        (_node_body([{"id": "ledger", "props": {"graphos_x": 1}}]), HTTPStatus.BAD_REQUEST),
        ({**_node_body([]), "node": {"label": "Service", "id": "ghost"}}, HTTPStatus.NOT_FOUND),
    ]
    for body, expected in cases:
        r = await api.post("/edges:reconcile", json=body)
        assert r.status_code == expected, (body, r.text)
    no_header = await api.post(
        "/edges:reconcile", content='{"src_id": "a", "dst_id": "b"}\n',
        headers={"content-type": "application/x-ndjson"},
    )
    assert no_header.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@requires_db
@pytest.mark.asyncio
async def test_resolve_by_difference_removes_only_what_a_rule_no_longer_derives(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Tag", rows=[
        {"id": t, "props": {"name": t}} for t in ("red", "blue", "green")
    ])
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
        {"id": "d1", "props": {"tags": ["red", "blue"]}},
    ])
    first = await api.post("/edges:resolve", json={"edge_types": ["TAGGED"]})
    assert first.json()["total_edges_written"] == 2  # noqa: PLR2004
    stored = await _edges(db_pool, "TAGGED")
    assert all(props == {"graphos_derived": True} for _, props in stored.values())
    # A consumer's own edge of the same type, and the document drops a tag.
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Doc", dst_label="Tag", edge_type="TAGGED",
        rows=[{"src_id": "d1", "dst_id": "green", "props": {"by": "hand"}}],
    )
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
        {"id": "d1", "props": {"tags": ["red"]}},
    ])
    insert_only = await api.post("/edges:resolve", json={"edge_types": ["TAGGED"]})
    assert insert_only.json()["total_edges_removed"] == 0
    dry = await api.post("/edges:resolve", json={
        "edge_types": ["TAGGED"], "mode": "reconcile", "dry_run": True,
    })
    assert dry.json()["results"][0]["edges_removed"] == 1
    assert len(await _edges(db_pool, "TAGGED")) == 3  # noqa: PLR2004
    done = await api.post("/edges:resolve", json={"edge_types": ["TAGGED"], "mode": "reconcile"})
    assert done.status_code == HTTPStatus.OK, done.text
    assert done.json()["total_edges_removed"] == 1
    assert set(await _edges(db_pool, "TAGGED")) == {("d1", "red"), ("d1", "green")}
    # Adopting takes the consumer's unmarked edge as the rule's: no rule derives it.
    adopt = await api.post("/edges:resolve", json={
        "edge_types": ["TAGGED"], "mode": "reconcile", "adopt_unmarked": True,
    })
    assert adopt.json()["total_edges_removed"] == 1
    assert set(await _edges(db_pool, "TAGGED")) == {("d1", "red")}
    refused = await api.post("/edges:resolve", json={"edge_types": ["TAGGED"], "dry_run": True})
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
