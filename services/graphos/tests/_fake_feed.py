"""An in-memory stand-in for the change-feed tables, for unit tests.

It answers the statements :mod:`graphos.changes` sends with what Postgres
would return for them: rows of one tenant, ordered by ``(txid, seq)``, only
those of transactions older than ``xmin``, and the tenant's horizon.  The
SQL itself is checked against a live database by ``test_changes.py``; this
fake lets the Python around it (cursors, paging, 410s, the HTTP routes and
the stream) run without one.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from graphos import changes

AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass
class FakeFeedDB:
    rows: list[dict[str, Any]] = field(default_factory=list)
    horizons: dict[str, tuple[int, int]] = field(default_factory=dict)
    xmin: int = 10**12
    executed: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    reads: list[tuple[Any, ...]] = field(default_factory=list)
    fail_reads_with: BaseException | None = None

    def add(  # noqa: PLR0913 — one record's fields
        self, tenant: str, txid: int, seq: int, *, op: str = "upsert", kind: str = "node",
        label: str | None = "Doc", ids: list[Any] | None = None, count: int = 1,
        detail: dict[str, Any] | None = None, consumer: str | None = None,
        trace_id: str | None = None, rev: int | None = None,
        src_label: str | None = None, dst_label: str | None = None,
    ) -> None:
        self.rows.append({
            "tenant": tenant, "txid": txid,
            "txid_text": str(txid), "seq": seq, "at": AT, "op": op, "kind": kind,
            "label": label, "src_label": src_label, "dst_label": dst_label,
            "ids_text": None if ids is None else json.dumps(ids), "rev": rev, "count": count,
            "detail_text": None if detail is None else json.dumps(detail),
            "consumer": consumer, "trace_id": trace_id,
        })

    def prune_through(self, tenant: str, txid: int, seq: int) -> None:
        """Delete ``tenant``'s records up to ``(txid, seq)`` and raise its horizon."""
        self.rows = [
            r for r in self.rows
            if r["tenant"] != tenant or (r["txid"], r["seq"]) > (txid, seq)
        ]
        self.horizons[tenant] = (txid, seq)

    # ── asyncpg.Connection surface ─────────────────────────────────────

    def _visible(self, tenant: str) -> list[dict[str, Any]]:
        return sorted(
            (r for r in self.rows if r["tenant"] == tenant and r["txid"] < self.xmin),
            key=lambda r: (r["txid"], r["seq"]),
        )

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.fail_reads_with is not None:
            raise self.fail_reads_with
        self.reads.append(args)
        if sql is changes._READ_SQL:  # noqa: SLF001
            tenant, txid_text, seq, limit = args
            after = (int(txid_text), seq)
            return [r for r in self._visible(tenant) if (r["txid"], r["seq"]) > after][:limit]
        if sql is changes._HISTORY_SQL:  # noqa: SLF001
            tenant, txid_text, seq, limit, ops = args
            rows = list(reversed(self._visible(tenant)))
            if txid_text is not None:
                rows = [r for r in rows if (r["txid"], r["seq"]) < (int(txid_text), seq)]
            if ops is not None:
                rows = [r for r in rows if r["op"] in ops]
            return rows[:limit]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "change_horizon" in sql:
            (tenant,) = args
            h = self.horizons.get(tenant)
            return None if h is None else {"txid_text": str(h[0]), "seq": h[1]}
        if "coalesce(sum(r.count)" in sql:
            tenant, txid_text, seq, ops, cap = args
            after = (int(txid_text), seq)
            hits = [
                r for r in sorted(self.rows, key=lambda r: (r["txid"], r["seq"]))
                if r["tenant"] == tenant and (r["txid"], r["seq"]) > after and r["op"] in ops
            ][:cap]
            return {"n": len(hits), "rows": sum(r["count"] for r in hits)}
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_snapshot_xmin" in sql and not args:
            return str(self.xmin)
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "SELECT 1"


class FakePool:
    """``pool.acquire()`` hands out the one fake connection."""

    def __init__(self, db: FakeFeedDB) -> None:
        self.db = db
        self.acquired = 0
        self.released = 0

    @asynccontextmanager
    async def _conn(self):  # noqa: ANN202
        self.acquired += 1
        try:
            yield self.db
        finally:
            self.released += 1

    def acquire(self):  # noqa: ANN201
        return self._conn()
