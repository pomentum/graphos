"""Two real graphos processes on one database: changes through one reach the other.

DB-gated.  Starts two API processes (uvicorn, auth disabled) against the
test database.  Their periodic registry sync and cache max age are set to
an hour, so anything that shows up on the other replica within seconds got
there by notification; the database lookup for an unknown tenant is tested
with a registration that is never announced.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from .conftest import requires_db

SERVICE = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(port: int) -> subprocess.Popen:
    dsn = urlsplit(os.environ["GRAPHOS_TEST_DSN"])
    env = {
        **os.environ,
        "GRAPHOS_DB_HOST": dsn.hostname or "127.0.0.1",
        "GRAPHOS_DB_PORT": str(dsn.port or 5432),
        "GRAPHOS_DB_NAME": dsn.path.lstrip("/"),
        "GRAPHOS_DB_USER": dsn.username or "graphos",
        "GRAPHOS_DB_PASSWORD": dsn.password or "",
        "GRAPHOS_AUTH_DISABLED": "true",
        "GRAPHOS_REGISTRY_SYNC_S": "3600",
        "GRAPHOS_CACHE_MAX_AGE_S": "3600",
        "GRAPHOS_LOG_LEVEL": "warning",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "graphos.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=SERVICE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture(scope="module")
def replicas() -> Iterator[tuple[httpx.Client, httpx.Client]]:
    if not os.environ.get("GRAPHOS_TEST_DSN"):
        pytest.skip("no GRAPHOS_TEST_DSN")
    ports = [_free_port(), _free_port()]
    procs = [_start(p) for p in ports]
    clients = [httpx.Client(base_url=f"http://127.0.0.1:{p}", timeout=30) for p in ports]
    try:
        for client, proc in zip(clients, procs, strict=True):
            deadline = time.monotonic() + 60
            while True:
                assert proc.poll() is None, "replica exited during startup"
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "replica did not become ready"
                time.sleep(0.25)
        yield clients[0], clients[1]
    finally:
        for client in clients:
            client.close()
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=20)


def _until(fn, timeout: float = 5.0) -> float:  # noqa: ANN001
    started = time.monotonic()
    while not fn():
        assert time.monotonic() - started < timeout, "not seen on the other replica in time"
        time.sleep(0.05)
    return time.monotonic() - started


def _tenant() -> str:
    return f"repl_{uuid4().hex[:10]}"


@requires_db
def test_a_registration_reaches_the_other_replica(replicas) -> None:  # noqa: ANN001
    a, b = replicas
    t = _tenant()
    assert a.post("/v1/tenants", json={"name": t, "vertex_labels": ["Doc"]}).status_code == 200
    took = _until(lambda: b.post(f"/v1/{t}/graph/nodes", json={
        "nodes": [{"id": "n1", "label": "Doc", "properties": {}}],
    }).status_code == 200)
    assert took < 3

    # A label added through A is writable through B.
    assert a.post("/v1/tenants", json={"name": t, "vertex_labels": ["Doc", "Tag"]}
                  ).status_code == 200
    _until(lambda: b.post(f"/v1/{t}/graph/nodes", json={
        "nodes": [{"id": "x", "label": "Tag", "properties": {}}],
    }).status_code == 200)

    # Unregistered through B: A stops serving it.
    assert b.delete(f"/v1/tenants/{t}").status_code == 200
    _until(lambda: a.get(f"/v1/{t}/graph/nodes/n1").status_code == 404)


@requires_db
def test_cache_writes_reach_the_other_replica(replicas) -> None:  # noqa: ANN001
    a, b = replicas
    t = _tenant()
    a.post("/v1/tenants", json={"name": t, "vertex_labels": ["Doc"]})
    _until(lambda: b.get(f"/v1/{t}/graph/catalogue").status_code == 200)

    assert a.put(f"/v1/{t}/graph/cache/k", json={"value": 1}).status_code == 200
    assert b.get(f"/v1/{t}/graph/cache/k").json()["value"] == 1     # now in B's memory
    assert b.get(f"/v1/{t}/graph/cache/k").json()["source"] == "lru"
    a.put(f"/v1/{t}/graph/cache/k", json={"value": 2})
    _until(lambda: b.get(f"/v1/{t}/graph/cache/k").json()["value"] == 2)
    a.delete(f"/v1/{t}/graph/cache/k")
    _until(lambda: b.get(f"/v1/{t}/graph/cache/k").status_code == 404)


@requires_db
def test_an_unannounced_registration_is_found_on_first_use(replicas) -> None:  # noqa: ANN001
    import asyncio

    import asyncpg

    from graphos import bootstrap

    a, b = replicas
    t = _tenant()

    async def register_silently() -> None:
        # As if the announcement was lost: the tables (bootstrap does not
        # announce) and the row, written without a NOTIFY.
        spec = bootstrap.TenantSpec(name=t, vertex_labels=("Doc",), edge_labels=())
        pool = await asyncpg.create_pool(
            os.environ["GRAPHOS_TEST_DSN"], min_size=1, max_size=1,
            setup=lambda c: c.execute('SET search_path = ag_catalog, "$user", public;'),
        )
        try:
            await bootstrap.bootstrap_tenant(pool, spec)
            await pool.execute(
                "INSERT INTO graphos_meta.tenants (name, spec) VALUES ($1, $2::jsonb);",
                t, bootstrap.spec_to_json(spec),
            )
        finally:
            await pool.close()

    asyncio.run(register_silently())
    # Neither replica was told; the first request on each loads it at once.
    for replica in (a, b):
        resp = replica.post(f"/v1/{t}/graph/nodes", json={
            "nodes": [{"id": "n1", "label": "Doc", "properties": {}}],
        })
        assert resp.status_code == 200, resp.text
