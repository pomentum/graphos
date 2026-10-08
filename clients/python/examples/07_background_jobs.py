"""Run long operations as background jobs: submit, poll, cancel.

Community recompute, rule-based edge resolution and vacuum can take minutes
on a large tenant.  The ``submit_*`` methods send ``Prefer: respond-async``:
graphos validates the request, queues it and answers at once with a job;
``wait_for_job`` polls it to the end.  Run 02_bulk_load.py first.

    python clients/python/examples/07_background_jobs.py
"""

from __future__ import annotations

import asyncio
import os
from http import HTTPStatus

from graphos_client import GraphosClient, GraphosError

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
# Signs in as GRAPHOS_USER / GRAPHOS_PASSWORD and renews its token by itself.


async def main() -> None:
    async with GraphosClient(base_url=URL, tenant="acme") as g:
        # Vacuum works on every build; a dry-run recompute needs the Louvain extra.
        job = await g.submit_vacuum(tables=["graphos_meta.jobs"])
        print(f"queued {job.kind} job {job.id} ({job.status})")
        job = await g.wait_for_job(job.id, poll_s=0.5, timeout_s=300)
        print(f"  -> {job.status}: {job.result}")

        if not (await g.info()).get("features", {}).get("louvain"):
            print("this server was built without the Louvain extra; skipping recompute")
            return

        job = await g.submit_recompute_communities(
            rel_types=["FOLLOWS", "AUTHORED"], min_community_size=3, dry_run=True,
        )
        # One recompute per tenant may be queued or running at a time.
        try:
            await g.submit_recompute_communities(dry_run=True)
        except GraphosError as exc:
            if exc.status_code != HTTPStatus.CONFLICT:
                raise
            print(f"second recompute refused while {job.id} is pending: {exc.status_code}")

        job = await g.wait_for_job(job.id, poll_s=0.5, timeout_s=600)
        if job.status == "succeeded":
            print(f"  -> {job.result['n_communities']} communities, "
                  f"modularity {job.result['modularity']:.3f}")
        else:
            print(f"  -> {job.status}: {job.error}")

        # A queued or running job can be cancelled; a finished one answers 409.
        job = await g.submit_recompute_communities(dry_run=True)
        job = await g.cancel_job(job.id)
        print(f"cancel requested: {job.status} (cancel_requested={job.cancel_requested})")
        job = await g.wait_for_job(job.id, poll_s=0.5, timeout_s=600)
        print(f"  -> {job.status}")

        recent = await g.list_jobs(tenant="acme", limit=5)
        print("recent:", [(j.kind, j.status) for j in recent])


if __name__ == "__main__":
    asyncio.run(main())
