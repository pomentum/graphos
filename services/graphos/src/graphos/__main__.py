"""Entry point.

- ``python -m graphos``: serve the API (uvicorn).  SIGTERM or SIGINT starts the
  drain (:mod:`graphos.drain`, ADR-0015) instead of uvicorn's own stop, which
  would close the socket at once and answer 500 to the requests it cuts; a
  second signal cuts the drain short.  The exit status is the drain's: 0, or 1
  when a job could not be handed back or the drain overran its grace.
- ``python -m graphos reset-admin-password``: give the bootstrap admin
  (``graphos``) a new random password, printed once, which must be changed at
  the next sign-in.  Every session and token of that user stops working; a
  disabled or deleted one is restored with the ``admin`` grant.  Run it where graphos runs
  (``docker compose exec graphos-api python -m graphos reset-admin-password``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import threading
from types import FrameType

import uvicorn
from uvicorn.config import STARTUP_FAILURE

from graphos import drain, identity
from graphos.config import Settings
from graphos.db import create_pool, ensure_internal_schema

USAGE = "usage: python -m graphos [reset-admin-password]\n"

log = logging.getLogger("graphos.drain")


class GraphosServer(uvicorn.Server):
    """uvicorn's server, stopped through the process's drain.

    uvicorn's own ``handle_exit`` sets ``should_exit``, and its stop then
    closes the listening socket at once; a second SIGTERM does nothing; after
    the stop it raises the caught signal again, so the exit status says
    "killed".  Here a signal goes to :meth:`graphos.drain.Drain.signal`, which
    sets ``should_exit`` itself once it is done (``on_done``), with what is
    left of the grace as uvicorn's last wait.  A signal before the server has
    started takes uvicorn's way.
    """

    def __init__(self, config: uvicorn.Config, *, watchdog: bool = True) -> None:
        super().__init__(config)
        # The drain of the app this server loaded (graphos.app.create_app installs it).
        self.drain: drain.Drain | None = None
        # False for a process that goes on after the server (tests): the
        # watchdog ends the process.
        self._arm_watchdog = watchdog
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog: threading.Timer | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self.drain = drain.current()
        if self.drain is not None:
            self.drain.on_done = self._exit
        await super().startup(sockets=sockets)

    def _exit(self, seconds_left: float) -> None:
        # Half for uvicorn's own wait (nothing should be open by now), half for
        # the lifespan's shutdown: the runner, the listener, the pools.
        self.config.timeout_graceful_shutdown = seconds_left / 2  # type: ignore[assignment]  # a float timeout works; uvicorn types it int
        self.should_exit = True

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        current = self.drain
        if current is None or self._loop is None or not self.started:
            super().handle_exit(sig, frame)
            return
        # Nothing more here: a signal handler interrupts the main thread
        # anywhere, and starting a thread from it could deadlock.
        self._loop.call_soon_threadsafe(self._signalled, current, signal.Signals(sig).name)

    def _signalled(self, current: drain.Drain, name: str) -> None:
        if self._arm_watchdog and self._watchdog is None:
            self._watchdog = threading.Timer(current.grace_s, _overran, args=(current.grace_s,))
            self._watchdog.daemon = True
            self._watchdog.start()
        current.signal(name)

    def disarm_watchdog(self) -> None:
        """The process is about to exit by itself."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None


def _overran(grace_s: float) -> None:
    """The drain's last resort, from a thread: the event loop may be stuck."""
    log.error("drain did not finish in time; exiting", extra={"grace_s": grace_s})
    logging.shutdown()
    os._exit(1)


def serve() -> None:
    settings = Settings()
    config = uvicorn.Config(
        "graphos.app:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
        # access logs go through structlog; disable uvicorn's default
        access_log=False,
        # X-Forwarded-* is honoured only from these addresses (comma-separated;
        # default: localhost).  Set GRAPHOS_FORWARDED_ALLOW_IPS to your reverse
        # proxy's address when running behind one.
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        # Longer than a proxy's idle timeout, so the proxy closes first
        # (GRAPHOS_KEEP_ALIVE_S; see config.py).
        timeout_keep_alive=settings.keep_alive_s,
        # The drain sets this to what is left of its grace when it is done;
        # this bounds a stop that did not come through it.
        timeout_graceful_shutdown=settings.shutdown_grace_s,  # type: ignore[arg-type]  # a float timeout works; uvicorn types it int
    )
    server = GraphosServer(config)
    server.run()
    # Done: the event loop is closed and the process exits now.
    server.disarm_watchdog()
    if not server.started:
        sys.exit(STARTUP_FAILURE)
    sys.exit(server.drain.exit_code if server.drain is not None else 0)


async def reset_admin_password(settings: Settings | None = None) -> str:
    """Set and return a new random password for the bootstrap admin."""
    settings = settings or Settings()
    pool = await create_pool(settings)
    try:
        await ensure_internal_schema(pool)
        store = identity.AccessStore(pool)
        await store.load()
        secret = identity.new_secret()
        name = identity.ADMIN_USER
        if store.user(name) is None:
            await store.create_user(
                name, "person", by="reset-admin-password", password=secret, must_change=True,
            )
        else:
            await store.set_password(name, secret, must_change=True)
            await store.set_disabled(name, False)
        await store.grant(identity.subject_of(name), "*", "admin", by="reset-admin-password")
        return secret
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        serve()
    elif args == ["reset-admin-password"]:
        secret = asyncio.run(reset_admin_password())
        sys.stdout.write(
            f"New password for '{identity.ADMIN_USER}' (shown once; it must be changed at the next "
            f"sign-in):\n{secret}\n",
        )
    else:
        sys.stderr.write(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
