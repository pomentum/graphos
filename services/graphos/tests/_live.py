"""A real HTTP server for tests that need one (streaming responses).

httpx's ASGI transport buffers a whole response, so a never-ending SSE
stream cannot be tested through it.  :func:`live_server` runs uvicorn on a
free local port in the test's own event loop, where the test's asyncpg pool
lives, and the change feed's listener when the test starts one.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import uvicorn
from fastapi import FastAPI

from graphos.app import create_app
from graphos.changes import ChangeFeed
from graphos.config import Settings
from graphos.sync import Listener


@asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[str]:
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
        timeout_graceful_shutdown=2,     # as in production: streams do not end
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@dataclass
class Stack:
    url: str
    pool: asyncpg.Pool
    feed: ChangeFeed | None


@asynccontextmanager
async def stack(
    *, feed: bool = True, poll_s: float = 1.0, **settings: object,
) -> AsyncIterator[Stack]:
    """App + its own pool (the feed's listener holds a connection) + server."""
    pool = await asyncpg.create_pool(
        os.environ["GRAPHOS_TEST_DSN"], min_size=2, max_size=16,
        setup=lambda c: c.execute('SET search_path = ag_catalog, "$user", public;'),
    )
    app = create_app(Settings(
        db_password="placeholder", auth_disabled=True, changes_poll_s=poll_s, **settings,
    ))
    app.state.pool = pool
    change_feed = listener = None
    if feed:
        listener = Listener(pool)
        change_feed = ChangeFeed(
            pool, listener, retention_h=168, poll_s=poll_s, prune_every_s=3600,
        )
        listener.start()
        change_feed.start()
        await asyncio.wait_for(listener.listening.wait(), timeout=10)
        app.state.change_feed = change_feed
    try:
        async with live_server(app) as url:
            yield Stack(url, pool, change_feed)
    finally:
        if change_feed is not None:
            await change_feed.stop()
        if listener is not None:
            await listener.stop()
        await pool.close()
