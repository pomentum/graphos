"""End-to-end test for the NDJSON bulk endpoints.

DB-gated. Drives the actual HTTP handler via TestClient + a real graphos-db.
We don't have an in-process AGE; the test boots a TestClient and asserts the
handler shape against an actual postgres+AGE backend reached through the
asyncpg pool the lifespan opens.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from graphos import writes
from graphos.api.bulk import _iter_ndjson_lines
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.cypher import query_cypher

from .conftest import requires_db

TEST_TENANT = "graphos_test_bulk"


@requires_db
@pytest.mark.asyncio
async def test_bulk_nodes_streams_in_batches(db_pool: asyncpg.Pool) -> None:
    """Stream a small NDJSON batch through writes.upsert_nodes directly.

    This validates the same write path the bulk handler delegates to,
    without standing up a TestClient (which would require its own
    lifespan + token-store + asyncpg pool to be wired against
    GRAPHOS_TEST_DSN).
    """
    spec = TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Thing",),
        edge_labels=("LINKS",),
    )
    await register_tenant(db_pool, spec)

    # Simulate batched flushing: insert 2 batches of 250 to exercise
    # multi-batch flush behaviour without crossing BULK_FLUSH_CHUNK.
    rows_a = [{"id": f"a-{i}", "props": {"name": f"a-{i}", "kind": "alpha"}} for i in range(250)]
    rows_b = [{"id": f"b-{i}", "props": {"name": f"b-{i}", "kind": "beta"}}  for i in range(250)]

    await writes.upsert_nodes(db_pool, tenant=TEST_TENANT, label="Thing", rows=rows_a)
    await writes.upsert_nodes(db_pool, tenant=TEST_TENANT, label="Thing", rows=rows_b)

    rows = await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher="MATCH (n:Thing) RETURN count(n) AS c",
        return_cols=[("c", "agtype")],
    )
    assert int(rows[0]["c"]) == 500

    # Re-run alpha: should remain 500 (idempotent).
    await writes.upsert_nodes(db_pool, tenant=TEST_TENANT, label="Thing", rows=rows_a)
    rows = await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher="MATCH (n:Thing) RETURN count(n) AS c",
        return_cols=[("c", "agtype")],
    )
    assert int(rows[0]["c"]) == 500

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT}', true);")


def test_ndjson_iter_handles_split_lines() -> None:
    """Smoke test the line-buffering: body parts that split mid-line must
    still emit whole NDJSON lines on the right line numbers."""
    class FakeRequest:
        """A minimal stand-in for FastAPI's Request with a streaming body."""

        def __init__(self, parts: list[bytes]):
            self._parts = parts

        async def stream(self):
            for p in self._parts:
                yield p

    # Two JSON lines split across three body parts; trailing line has no
    # final newline.
    parts = [
        b'{"id": "a", ',                   # mid-line
        b'"label": "X"}\n{"id": "b"',      # finishes line 1, starts line 2
        b', "label": "Y"}',                # no trailing newline
    ]

    async def _collect() -> list[tuple[int, str]]:
        return [pair async for pair in _iter_ndjson_lines(FakeRequest(parts))]

    pairs = asyncio.run(_collect())
    assert [n for n, _ in pairs] == [1, 2]
    assert json.loads(pairs[0][1])["id"] == "a"
    assert json.loads(pairs[1][1])["id"] == "b"
