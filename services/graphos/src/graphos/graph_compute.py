"""Whole-graph computations in a child process: Louvain and graph metrics.

What every such computation shares: the tenant's edges as compact columns
(:class:`EdgeArrays`), and one child process at a time per API process
(:func:`run_in_child`). The child imports only the module of the function it
runs (``graphos.louvain_compute`` with igraph, or ``graphos.graph_metrics_compute``
with rustworkx), so this module stays free of service code.

Why a child process per run:

* the libraries hold Python's GIL for the whole computation; in the API
  process that would stall every request and job heartbeat for as long;
* cancelling terminates the child, which stops the computation and frees its
  memory at once, so the next run does not wait behind it;
* memory goes back to the system after every run.

One computation at a time per API process, whatever its kind, bounds memory
to one in-memory graph (the API container is sized for that).
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import multiprocessing
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any


@dataclass
class EdgeArrays:
    """The tenant's edges as compact columns: ``src[i]``/``dst[i]`` are
    graphids, ``rel[i]`` indexes ``rel_names``.  About 20 bytes per edge,
    against well over 100 for a Python tuple, and cheap to hand to the
    child process."""

    src: array.array[int] = field(default_factory=lambda: array.array("q"))
    dst: array.array[int] = field(default_factory=lambda: array.array("q"))
    rel: array.array[int] = field(default_factory=lambda: array.array("I"))
    rel_names: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.src)


def _child(conn: Connection, target: Callable[..., Any]) -> None:
    try:
        edges, options = conn.recv()
        conn.send(("ok", target(edges, **options)))
    except BaseException as exc:  # noqa: BLE001 — report every failure to the parent
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _receive(conn: Connection, what: str) -> Any:
    try:
        status, payload = conn.recv()
    except EOFError:
        raise RuntimeError(
            f"the {what} process ended without a result (killed, or out of memory)"
        ) from None
    if status != "ok":
        raise RuntimeError(f"{what} computation failed: {payload}")
    return payload


_one_at_a_time = asyncio.Lock()
_running: multiprocessing.process.BaseProcess | None = None


async def run_in_child(
    target: Callable[..., Any], edges: EdgeArrays, options: dict[str, Any], *, what: str,
) -> Any:
    """``target(edges, **options)`` in a child process, one at a time.

    ``target`` must be a module-level function (the child imports it by
    name). Cancelling the awaiting task terminates the child.
    """
    global _running  # noqa: PLW0603 — the one child of this process, for shutdown
    async with _one_at_a_time:
        ctx = multiprocessing.get_context("spawn")
        conn, child_conn = ctx.Pipe()
        proc = ctx.Process(target=_child, args=(child_conn, target), daemon=True)
        _running = proc
        # Starting a spawn child and sending it the edges both block until
        # the new interpreter reads them: keep both off the event loop.  The
        # start is shielded so a cancel cannot leave a child starting unseen.
        start = asyncio.ensure_future(asyncio.to_thread(proc.start))
        try:
            await asyncio.shield(start)
            await asyncio.to_thread(conn.send, (edges, options))
            return await asyncio.to_thread(_receive, conn, what)
        finally:
            with contextlib.suppress(BaseException):
                await start               # cancelled mid-start: let it finish
            child_conn.close()
            if proc.is_alive():
                proc.terminate()          # cancelled, or the result is already in
            if proc.pid is not None:
                await asyncio.to_thread(proc.join, 10)
            conn.close()
            _running = None


def running() -> multiprocessing.process.BaseProcess | None:
    """The child computing now, if any."""
    return _running


def terminate_running() -> None:
    """Stop the computation in progress, if any (API shutdown)."""
    if _running is not None and _running.is_alive():
        _running.terminate()
