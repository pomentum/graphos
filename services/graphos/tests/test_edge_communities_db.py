"""The edge community write-back writes each edge at most once.

Both columns of an edge, ``start_community_id`` and ``end_community_id``,
are set by one UPDATE; an edge whose two ends both moved was written twice
(once per end), each write a whole new row version. DB-gated.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos import writes
from graphos.bootstrap import TenantSpec, register_tenant

from .conftest import requires_db

TENANT = "graphos_test_edge_communities"
NODES = ("a1", "a2", "a3", "b1", "b2", "c1")
EDGES = (("a1", "a2"), ("a2", "a3"), ("b1", "a1"), ("a2", "b2"), ("b2", "b1"), ("c1", "a1"))
# c1 is in no community: it is never staged.
COMMUNITY = {"a1": "1", "a2": "1", "a3": "1", "b1": "2", "b2": "2"}


async def _seed(pool: asyncpg.Pool) -> dict[str, int]:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Node",), edge_labels=("LINKS",),
    ))
    async with pool.acquire() as conn:
        for table in ("LINKS", "Node"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{table}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Node",
                              rows=[{"id": n, "props": {}} for n in NODES])
    await writes.upsert_edges(pool, tenant=TENANT, src_label="Node", dst_label="Node",
                              edge_type="LINKS",
                              rows=[{"src_id": s, "dst_id": d, "props": {}} for s, d in EDGES])
    rows = await pool.fetch(
        "SELECT (id::text)::bigint AS gid, "
        "       trim(both '\"' from (properties->'\"id\"')::text) AS ext "
        f'FROM "{TENANT}"."Node";',
    )
    return {r["ext"]: int(r["gid"]) for r in rows}


async def _columns(pool: asyncpg.Pool, gids: dict[str, int]) -> dict[tuple[str, str], tuple]:
    ext = {g: n for n, g in gids.items()}
    rows = await pool.fetch(
        "SELECT (start_id::text)::bigint AS s, (end_id::text)::bigint AS e, "
        f'start_community_id, end_community_id FROM "{TENANT}"."LINKS";',
    )
    return {(ext[r["s"]], ext[r["e"]]): (r["start_community_id"], r["end_community_id"])
            for r in rows}


async def _write(pool: asyncpg.Pool, gids: dict[str, int], community: dict[str, str]) -> int:
    return await writes.set_edge_end_communities(
        pool, tenant=TENANT, assignments=((gids[n], c) for n, c in community.items()),
    )


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("chunk", [50_000, 2])   # one range per label, and several
async def test_each_edge_is_written_once_with_both_ends(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, chunk: int,
) -> None:
    monkeypatch.setattr(writes, "EDGE_COMMUNITY_CHUNK", chunk)
    gids = await _seed(db_pool)

    # Every edge has a staged end, so every edge is written, once: a statement
    # per end wrote the 5 edges with two staged ends twice (11 rows).
    assert await _write(db_pool, gids, COMMUNITY) == len(EDGES)
    assert await _columns(db_pool, gids) == {
        ("a1", "a2"): ("1", "1"), ("a2", "a3"): ("1", "1"), ("b1", "a1"): ("2", "1"),
        ("a2", "b2"): ("1", "2"), ("b2", "b1"): ("2", "2"),
        ("c1", "a1"): (None, "1"),          # the unstaged end is left alone
    }

    # Nothing moved: nothing is written.
    assert await _write(db_pool, gids, COMMUNITY) == 0

    # b2 moves: only its two edges are written, each once.
    assert await _write(db_pool, gids, {**COMMUNITY, "b2": "1"}) == 2
    after = await _columns(db_pool, gids)
    assert after[("a2", "b2")] == ("1", "1")
    assert after[("b2", "b1")] == ("1", "2")
    assert after[("c1", "a1")] == (None, "1")


@requires_db
@pytest.mark.asyncio
async def test_an_end_that_leaves_every_community_keeps_its_value(db_pool: asyncpg.Pool) -> None:
    # As before: a vertex no longer staged (its community fell below the
    # minimum size) does not clear its edges' column; only staged ends write.
    gids = await _seed(db_pool)
    await _write(db_pool, gids, COMMUNITY)
    without_b = {n: c for n, c in COMMUNITY.items() if not n.startswith("b")}
    assert await _write(db_pool, gids, without_b) == 0
    assert (await _columns(db_pool, gids))[("b2", "b1")] == ("2", "2")
