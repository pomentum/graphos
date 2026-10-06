"""Entry point: `python -m graphos` boots the FastAPI app via uvicorn."""

from __future__ import annotations

import uvicorn

from graphos.config import Settings


def main() -> None:
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
        timeout_graceful_shutdown=settings.shutdown_grace_s,
    )


if __name__ == "__main__":
    main()
