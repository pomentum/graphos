"""Draining on SIGTERM: block new requests, finish the running ones, hand back the rest.

ADR-0015.  From the signal (*T*), bounded by ``GRAPHOS_SHUTDOWN_GRACE_S`` (*grace*):

1. **draining** (*T*): ``/readyz`` answers 503 ``draining``; the job runner stops
   claiming and marks the jobs it runs; every response carries ``Connection:
   close``, so uvicorn closes the connection after it and the client comes back
   on a new one, which a load balancer sends elsewhere.  Requests are still served.
2. **refusing** (*T* + ``GRAPHOS_DRAIN_DELAY_S``): every request but ``/healthz``,
   ``/readyz`` and ``/metrics`` gets 503 with ``Retry-After`` and ``code:
   "draining"``; change-feed streams end with ``event: draining``.  graphos keeps
   listening, so a client gets an answer rather than a refused connection.
3. **finishing**: requests in flight and running jobs go on, until none is left
   or until the cut, at *T* + *grace* − the reserve (:func:`graphos.config.drain_reserve_s`).
4. **cutting**: what still runs is cancelled.  A request whose response has not
   started gets the 503; one cut mid-body is closed by uvicorn.  Jobs are handed
   back to the queue (:meth:`graphos.jobs.JobRunner.hand_back_all`).  A
   transaction is atomic, so a write and its change record are both committed or
   both rolled back, whatever moment the cut hits.
5. **done**: the server is told to exit with what is left of the grace
   (:attr:`Drain.on_done`, set by :class:`graphos.__main__.GraphosServer`, which
   also arms a watchdog that ends the process at *T* + *grace*).

Why :class:`DrainMiddleware` marks the tasks it cancels instead of reading
``Task.cancelling()``: graphos's other middleware are Starlette
``BaseHTTPMiddleware``, which runs the handler in an anyio task group, and by
the time the ``CancelledError`` comes back out the task's cancel count is 0
again.  A check on it would let the error through, and uvicorn would answer 500
(measured, ADR-0015 E3; ``tests/test_drain.py``).

The rest of the process takes part through :class:`DrainHooks`, which the
lifespan sets (``graphos.app``).  A process without a server (tests) calls
:meth:`Drain.signal` itself.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from graphos.config import drain_reserve_s
from graphos.observability.metrics import DRAIN_REFUSED, DRAINING, REQUESTS_IN_FLIGHT

log = logging.getLogger(__name__)

Phase = Literal["serving", "draining", "refusing", "cutting", "done"]

# Answered all through a drain: the platform's probes and the metrics scrape.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
RETRY_AFTER_S = 2
_PROBLEM = json.dumps({
    "type": "about:blank",
    "title": "Shutting down",
    "status": 503,
    "detail": "graphos is shutting down; send the request again",
    "code": "draining",
}).encode()
_REFUSAL_HEADERS = [
    (b"content-type", b"application/problem+json"),
    (b"content-length", str(len(_PROBLEM)).encode()),
    (b"retry-after", str(RETRY_AFTER_S).encode()),
    (b"connection", b"close"),
]
STREAM_EVENT = 'event: draining\ndata: {"reason": "draining"}\n\n'


@dataclass
class DrainHooks:
    """What the drain asks of the rest of the process (set by the lifespan).

    ``begin``: stop claiming jobs and mark the running ones.  ``refuse``: wake
    the change-feed streams so they end.  ``cut``: hand the running jobs back,
    returning how many ended in each outcome (``error``: could not be written).
    ``running_jobs``: how many jobs this process runs now.
    """

    begin: Callable[[], Awaitable[None]] | None = None
    refuse: Callable[[], None] | None = None
    cut: Callable[[], Awaitable[Mapping[str, int]]] | None = None
    running_jobs: Callable[[], int] | None = None


class Drain:
    """One process's drain: its phase, the requests in flight, the outcome."""

    def __init__(self, *, grace_s: float, delay_s: float) -> None:
        self.grace_s = grace_s
        self.delay_s = delay_s
        self.reserve_s = drain_reserve_s(grace_s)
        self.phase: Phase = "serving"
        self.hooks = DrainHooks()
        # Called once the drain is over, with the seconds left of the grace.
        self.on_done: Callable[[float], None] | None = None
        self.exit_code = 0
        self.streams_ended = 0
        self._requests: dict[asyncio.Task[Any], str] = {}
        self._cut: set[asyncio.Task[Any]] = set()
        self._cut_short = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def draining(self) -> bool:
        """The drain has begun: ``/readyz`` says so, responses close their connection."""
        return self.phase != "serving"

    @property
    def refusing(self) -> bool:
        """New requests are refused and streams end."""
        return self.phase in ("refusing", "cutting", "done")

    @property
    def cutting(self) -> bool:
        """What still runs is being cancelled."""
        return self.phase in ("cutting", "done")

    @property
    def in_flight(self) -> int:
        return len(self._requests)

    def signal(self, name: str) -> None:
        """A stop signal: the first begins the drain, a later one cuts it short.

        Runs in the event loop (the server schedules it there)."""
        if self._task is None:
            # Draining from this moment, not from when the task first runs.
            self.phase = "draining"
            DRAINING.set(1)
            self._task = asyncio.get_running_loop().create_task(
                self._run(name), name="graphos-drain",
            )
        elif not self._cut_short.is_set():
            log.warning("drain cut short", extra={"signal": name})
            self._cut_short.set()

    async def wait(self) -> None:
        """Until the drain is over (tests)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    def stream_ended(self) -> None:
        self.streams_ended += 1

    def enter(self, task: asyncio.Task[Any], path: str) -> None:
        """A request is being served by ``task``."""
        self._requests[task] = path
        REQUESTS_IN_FLIGHT.inc()

    def leave(self, task: asyncio.Task[Any]) -> None:
        """The request served by ``task`` is over."""
        self._requests.pop(task, None)
        self._cut.discard(task)
        REQUESTS_IN_FLIGHT.dec()

    def was_cut(self, task: asyncio.Task[Any]) -> bool:
        """The drain cancelled ``task``: its ``CancelledError`` is the drain's."""
        return task in self._cut

    def _running_jobs(self) -> int:
        return self.hooks.running_jobs() if self.hooks.running_jobs is not None else 0

    def _idle(self) -> bool:
        return not self._requests and self._running_jobs() == 0

    async def _until(self, when: float, *, idle: bool = False) -> None:
        """Wait until ``when`` (loop time), a second signal, or, with ``idle``,
        nothing left to wait for."""
        loop = asyncio.get_running_loop()
        while loop.time() < when and not self._cut_short.is_set():
            if idle and self._idle():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._cut_short.wait(), min(0.05, max(0.0, when - loop.time())),
                )

    async def _bounded(self, work: Awaitable[Any], until: float, what: str) -> Any:
        """``work``, cut off at ``until`` (loop time).  ``None`` and exit code 1
        when it fails or does not finish: one step's failure must not stop the
        steps after it."""
        left = until - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(work, max(0.01, left))
        except TimeoutError:
            log.error("drain: %s did not finish in time", what)
        except Exception:
            log.exception("drain: %s failed", what)
        self.exit_code = 1
        return None

    def _cut_requests(self) -> int:
        for task in self._requests:
            self._cut.add(task)
            task.cancel()
        return len(self._cut)

    async def _run(self, signal_name: str) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + self.grace_s
        cut_at = deadline - self.reserve_s
        log.warning(
            "drain started",
            extra={
                "signal": signal_name, "grace_s": self.grace_s, "delay_s": self.delay_s,
                "in_flight": self.in_flight, "jobs_running": self._running_jobs(),
            },
        )
        cut_requests = 0
        outcomes: Mapping[str, int] = {}
        try:
            # Beside the phases, not before them: a database that does not
            # answer must not hold the refusals back.
            begun: asyncio.Future[None] | None = (
                asyncio.ensure_future(self.hooks.begin()) if self.hooks.begin is not None else None
            )
            await self._until(start + self.delay_s)
            self.phase = "refusing"
            log.info("drain refusing", extra={"in_flight": self.in_flight})
            if self.hooks.refuse is not None:
                self.hooks.refuse()
            await self._until(cut_at, idle=True)
            if begun is not None:
                await self._bounded(begun, cut_at, "stopping the job runner")
            if self._requests:
                self.phase = "cutting"
                cut_requests = self._cut_requests()
            if self.hooks.cut is not None:
                outcomes = await self._bounded(
                    self.hooks.cut(), deadline - self.reserve_s / 2, "handing jobs back",
                ) or {}
            if outcomes.get("error"):
                self.exit_code = 1
            # The cut requests answer their 503 and leave.
            await self._until(deadline - self.reserve_s / 2, idle=True)
        except Exception:
            log.exception("drain failed")
            self.exit_code = 1
        finally:
            self.phase = "done"
            jobs = collections.Counter(outcomes)
            log.warning(
                "drained",
                extra={
                    "waited_s": round(loop.time() - start, 2),
                    "cut": {
                        "requests": cut_requests,
                        "streams": self.streams_ended,
                        "jobs_queued": jobs["queued"],
                        "jobs_failed": jobs["failed"],
                        "jobs_cancelled": jobs["cancelled"],
                    },
                    "exit_code": self.exit_code,
                },
            )
            if self.on_done is not None:
                self.on_done(max(0.1, deadline - loop.time()))


async def _refuse(send: Send) -> None:
    DRAIN_REFUSED.inc()
    await send({"type": "http.response.start", "status": 503, "headers": _REFUSAL_HEADERS})
    await send({"type": "http.response.body", "body": _PROBLEM})


class DrainMiddleware:
    """Refuses requests once the drain refuses, closes connections once it has
    begun, and turns its own cancellation of a request into the 503."""

    def __init__(self, app: ASGIApp, drain: Drain) -> None:
        self.app = app
        self.drain = drain

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        drain = self.drain
        exempt = scope.get("path", "") in EXEMPT_PATHS
        if drain.refusing and not exempt:
            await _refuse(send)
            return
        started = False

        async def send_closing(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if drain.draining:
                    headers = [
                        (name, value) for name, value in message.get("headers", [])
                        if name.lower() != b"connection"
                    ]
                    message = {**message, "headers": [*headers, (b"connection", b"close")]}
            await send(message)

        if exempt:
            await self.app(scope, receive, send_closing)
            return
        task = asyncio.current_task()
        assert task is not None
        drain.enter(task, scope.get("path", ""))
        try:
            await self.app(scope, receive, send_closing)
        except asyncio.CancelledError:
            if not drain.was_cut(task):
                raise
            while task.cancelling():
                task.uncancel()
            if not started:
                await _refuse(send)
            # Cut mid-body: returning leaves the response incomplete, and
            # uvicorn closes the connection.
        finally:
            drain.leave(task)


# The process's drain, made by graphos.app.create_app; the server reads it.
_current: Drain | None = None


def install(drain: Drain | None) -> None:
    global _current  # noqa: PLW0603 — one drain per process
    _current = drain


def current() -> Drain | None:
    return _current


def cutting() -> bool:
    """This process's drain is cancelling what still runs."""
    return _current is not None and _current.cutting
