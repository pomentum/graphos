"""End-to-end tests for ``GET /v1/{tenant}/graph/communities``.

Exercises :func:`_communities_with_labels` — the single-SQL helper that
replaced the per-community ``_sample_labels`` N+1 (one Cypher per
community → one GROUP BY for the whole list).  These tests do not call
the FastAPI route directly; instead they invoke the helper against a
seeded graph and assert the response shape + label aggregation that
flows through into :class:`CommunitiesResponse`.

DB-gated like the rest of the suite: skipped unless ``GRAPHOS_TEST_DSN``
is set.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos import writes
from graphos.api.communities import _communities_with_labels
from graphos.bootstrap import TenantSpec, register_tenant

from .conftest import requires_db

TEST_TENANT = "graphos_test_communities"


class _FakeAppState:
    """Stand-in for ``request.app.state`` — only ``.pool`` is touched."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool


class _FakeApp:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.state = _FakeAppState(pool)


class _FakeRequest:
    """Minimal stand-in for FastAPI ``Request`` — helper only reads ``app.state.pool``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.app = _FakeApp(pool)


async def _seed(pool: asyncpg.Pool) -> None:
    """Two vertex labels across three communities of distinct sizes.

    Layout:
      * community 1 — 3 Documents
      * community 2 — 2 Documents + 1 Collection (mixed labels)
      * community 3 — 1 Document only
      * 1 unassigned vertex (no community_id)
    """
    spec = TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Document", "Collection"),
        edge_labels=("CITES",),
    )
    await register_tenant(pool, spec)

    # community_id as Louvain's write-back leaves it (an agtype integer under
    # properties.community_id), written with SQL as graphos writes: a Cypher
    # SET into graphos's tables fails on AGE 1.8 (graphos_rev comes out NULL).
    assignments = {
        "c1a": 1, "c1b": 1, "c1c": 1,
        "c2a": 2, "c2b": 2, "cmp2": 2,
        "c3a": 3,
        # "orphan" intentionally left without community_id
    }

    def rows(*ids: str) -> list[dict[str, object]]:
        return [
            {"id": i, "props": {"community_id": assignments[i]} if i in assignments else {}}
            for i in ids
        ]

    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Document",
        rows=rows("c1a", "c1b", "c1c", "c2a", "c2b", "c3a", "orphan"),
    )
    await writes.upsert_nodes(pool, tenant=TEST_TENANT, label="Collection", rows=rows("cmp2"))


async def _drop(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT}', true);")


@requires_db
@pytest.mark.asyncio
async def test_communities_single_query_shape(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    try:
        req = _FakeRequest(db_pool)
        triples, n_assigned, n_unassigned = await _communities_with_labels(
            req, TEST_TENANT, min_size=1, limit=None,
        )

        # Three communities, in size-descending order (3, 3, 1).
        assert len(triples) == 3
        sizes = [sz for _, sz, _ in triples]
        assert sizes == sorted(sizes, reverse=True)
        assert sizes == [3, 3, 1]

        # n_assigned counts every vertex with community_id (3+3+1=7);
        # n_unassigned counts the one orphan.  Total vertices = 8.
        assert n_assigned == 7
        assert n_unassigned == 1

        # Every community must have non-empty sample_labels — the
        # per-community aggregation runs in the same query, not N+1.
        for cid, sz, labels in triples:
            assert labels, (
                f"community {cid} (size {sz}) returned empty sample_labels "
                "— the array_agg(DISTINCT label) GROUP BY should always "
                "produce at least one label per row"
            )

        # Mixed community 2 should expose BOTH labels (Document + Collection);
        # the alphabetical ORDER BY makes the result stable.
        by_cid = {cid: labels for cid, _, labels in triples}
        assert by_cid[2] == ["Collection", "Document"]   # alphabetical (ORDER BY relname)
        # Pure-Document communities only see one label.
        assert by_cid[1] == ["Document"]
        assert by_cid[3] == ["Document"]
    finally:
        await _drop(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_communities_min_size_and_limit(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    try:
        req = _FakeRequest(db_pool)

        # min_size=2 drops community 3 (size 1) but keeps the assigned
        # totals intact (helper reports global n_assigned, not the
        # filtered sum).
        triples, n_assigned, n_unassigned = await _communities_with_labels(
            req, TEST_TENANT, min_size=2, limit=None,
        )
        assert [sz for _, sz, _ in triples] == [3, 3]
        assert n_assigned == 7  # global, not post-filter
        assert n_unassigned == 1

        # limit=1 returns only the top community (sized 3); the other
        # two are paginated off.  Global totals stay accurate so the
        # response can report the full counts.
        triples, n_assigned, n_unassigned = await _communities_with_labels(
            req, TEST_TENANT, min_size=1, limit=1,
        )
        assert len(triples) == 1
        assert triples[0][1] == 3
        assert n_assigned == 7
        assert n_unassigned == 1
    finally:
        await _drop(db_pool)
