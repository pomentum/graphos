"""Pytest fixtures.

Notes:
- DB-touching tests require a running graphos-db. They're skipped unless
  ``GRAPHOS_TEST_DSN`` is set in the environment.
- Pure-Python tests (auth, config, bootstrap-state) run unconditionally.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

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
