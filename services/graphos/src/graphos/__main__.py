"""Entry point.

- ``python -m graphos``: serve the API (uvicorn).
- ``python -m graphos reset-admin-password``: give the bootstrap admin
  (``graphos``) a new random password, printed once, which must be changed at
  the next sign-in.  Every session and token of that user stops working; a
  disabled or deleted one is restored with the ``admin`` grant.  Run it where graphos runs
  (``docker compose exec graphos-api python -m graphos reset-admin-password``).
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from graphos import identity
from graphos.config import Settings
from graphos.db import create_pool, ensure_internal_schema

USAGE = "usage: python -m graphos [reset-admin-password]\n"


def serve() -> None:
    settings = Settings()
    uvicorn.run(
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
        # A change-feed stream never ends by itself: without a limit, a
        # graceful stop would wait on every open stream until the container
        # is killed.  After this many seconds open requests are cancelled;
        # stream clients resume on another replica with Last-Event-ID.
        timeout_graceful_shutdown=settings.shutdown_grace_s,  # type: ignore[arg-type]  # a float timeout works; uvicorn types it int
    )


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
