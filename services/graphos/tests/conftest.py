"""Pytest fixtures.

Notes:
- DB-touching tests require a running graphos-db. They're skipped unless
  ``GRAPHOS_TEST_DSN`` is set in the environment.
- Pure-Python tests (auth, config, bootstrap-state) run unconditionally.
- A test run removes every tenant it registered and every graph it created
  (see ``_remove_what_the_run_created``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio

from graphos.bootstrap import unregister_tenant
from graphos.db import ensure_internal_schema


def _has_db() -> bool:
    return bool(os.environ.get("GRAPHOS_TEST_DSN"))


# `requires_db` is two markers in one:
#  - `pytest.mark.db`     → categorical, lets CI select / deselect via `-m`.
#  - `pytest.mark.skipif` → conditional, automatically skips when the env
#                           var is missing so a developer running `pytest`
#                           with no Docker around doesn't get red.
#
# Recipes:
#   pytest -m "not db"      fast path, never touches the DB
#   pytest -m db            CI integration run (must export GRAPHOS_TEST_DSN)
#   pytest                  run-everything; DB ones skip if no DSN
def requires_db(func):  # noqa: ANN001, ANN201 — decorator passthrough
    func = pytest.mark.skipif(
        not _has_db(),
        reason="Set GRAPHOS_TEST_DSN to enable DB-touching tests.",
    )(func)
    return pytest.mark.db(func)


@pytest_asyncio.fixture
async def db_pool() -> AsyncIterator[asyncpg.Pool]:
    """Create a small pool against GRAPHOS_TEST_DSN. Skip if unset."""
    if not _has_db():
        pytest.skip("no GRAPHOS_TEST_DSN")

    dsn = os.environ["GRAPHOS_TEST_DSN"]

    # Mirror the runtime pool (db.py): LOAD once per connection, but re-apply
    # search_path on every acquire — asyncpg's RESET ALL on release wipes it.
    async def _init(conn: asyncpg.Connection) -> None:
        await conn.execute("LOAD 'age';")

    async def _setup(conn: asyncpg.Connection) -> None:
        await conn.execute('SET search_path = ag_catalog, "$user", public;')

    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=2, init=_init, setup=_setup
    )
    assert pool is not None
    # graphos_meta tables (the change feed's included): every write records.
    await ensure_internal_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


# ── Tenants and graphs a test run creates ───────────────────────────────────
# graphos re-bootstraps every registered tenant when it starts, recreating a
# graph a test dropped, so tenants a run leaves registered pile up in the test
# database and slow down every start (the replica tests start API processes,
# which then miss their deadline). Graphs a run leaves without a registration
# (unregistering keeps the graph) pile up too. A run removes, at its end, every
# tenant registered and every graph created since it began; what was there
# before the run is left as it is.


async def _present(dsn: str) -> tuple[set[str], set[str]]:
    """The registered tenants and the AGE graphs."""
    conn = await asyncpg.connect(dsn)
    try:
        tenants: set[str] = set()
        if await conn.fetchval("SELECT to_regclass('graphos_meta.tenants') IS NOT NULL;"):
            rows = await conn.fetch("SELECT name FROM graphos_meta.tenants;")
            tenants = {r["name"] for r in rows}
        rows = await conn.fetch("SELECT name::text FROM ag_catalog.ag_graph;")
        graphs = {r["name"] for r in rows}
        return tenants, graphs
    finally:
        await conn.close()


async def _remove_since(dsn: str, before: tuple[set[str], set[str]]) -> None:
    tenants, graphs = await _present(dsn)
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=1)
    assert pool is not None
    try:
        for name in sorted(tenants - before[0]):
            await unregister_tenant(pool, name)
        async with pool.acquire() as conn:
            await conn.execute("LOAD 'age';")
            for name in sorted(graphs - before[1]):
                await conn.execute("SELECT ag_catalog.drop_graph($1::name, true);", name)
    finally:
        await pool.close()


@pytest.fixture(scope="session", autouse=True)
def _remove_what_the_run_created() -> Iterator[None]:
    if not _has_db():
        yield
        return
    dsn = os.environ["GRAPHOS_TEST_DSN"]
    before = asyncio.run(_present(dsn))
    yield
    asyncio.run(_remove_since(dsn, before))
