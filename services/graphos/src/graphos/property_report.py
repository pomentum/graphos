"""What a tenant's stored rows break of its property schema (``properties:check``).

Every row of the chosen labels and edge types is read once, through a
server-side cursor, and checked with the same compiled policies as a write,
as if every rule applied (whatever the tenant's mode). A node is read under
its own label only (``FROM ONLY``), so a parent label does not count its
children's nodes twice.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from typing import Any

import asyncpg

from graphos import properties
from graphos.contracts.property_schema import (
    LabelCheck,
    PropertiesCheckRequest,
    PropertiesCheckResponse,
)

# Rows fetched per round trip.
_FETCH = 2000
# Undeclared names listed per label, most frequent first.
_UNDECLARED_SHOWN = 50


class _Collector:
    """One label's counts, examples and undeclared names."""

    def __init__(self, kind: str, name: str, policy: properties.Policy, examples: int) -> None:
        self.kind, self.name, self.policy, self.examples = kind, name, policy, examples
        self.rows = self.with_errors = self.with_warnings = 0
        self.errors: Counter[str] = Counter()
        self.warnings: Counter[str] = Counter()
        self.example_refs: dict[str, list[str]] = defaultdict(list)
        self.undeclared: Counter[str] = Counter()

    def add(self, props: dict[str, Any], row_id: str | None, ref: str) -> None:
        self.rows += 1
        violations = self.policy.check(props, row_id=row_id)
        errors = [v for v in violations if v.severity == properties.ERROR]
        self.with_errors += bool(errors)
        self.with_warnings += len(errors) < len(violations)
        for v in violations:
            (self.errors if v.severity == properties.ERROR else self.warnings)[v.key] += 1
            refs = self.example_refs[v.key]
            if len(refs) < self.examples:
                refs.append(ref)
        declared = self.policy.properties
        self.undeclared.update(
            k for k in props if k not in declared and not properties.always_allowed(k)
        )

    def result(self, names: dict[str, str]) -> LabelCheck:
        def named(ref: str) -> str:
            if self.kind == "vertex":
                return ref
            return " -> ".join(names.get(gid, gid) for gid in ref.split(" -> "))
        return LabelCheck(
            kind=self.kind, name=self.name, rows=self.rows,
            rows_with_errors=self.with_errors, rows_with_warnings=self.with_warnings,
            errors=dict(self.errors.most_common()), warnings=dict(self.warnings.most_common()),
            examples={k: [named(r) for r in refs] for k, refs in self.example_refs.items()},
            undeclared=dict(self.undeclared.most_common(_UNDECLARED_SHOWN)),
        )


async def check_stored(
    pool: asyncpg.Pool, spec: Any, body: PropertiesCheckRequest,
) -> PropertiesCheckResponse:
    """The report on ``spec``'s stored rows (a ``TenantSpec``)."""
    started = time.perf_counter()
    tenant = spec.name
    policies = properties.policies_for(spec).with_mode("warn")
    labels = [lab for lab in spec.vertex_labels if body.labels is None or lab in body.labels]
    types = [t for t in spec.edge_labels if body.types is None or t in body.types]
    collectors: list[_Collector] = []
    edge_ends: set[str] = set()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        for label in labels:
            c = _Collector("vertex", label, policies.for_vertex(label), body.examples)
            async for row in conn.cursor(
                f'SELECT ((properties)::text)::jsonb AS props FROM ONLY "{tenant}"."{label}";',
                prefetch=_FETCH,
            ):
                props = json.loads(row["props"])
                c.add(props, props.get("id"), str(props.get("id")))
            collectors.append(c)
        for edge_type in types:
            c = _Collector("edge", edge_type, policies.for_edge(edge_type), body.examples)
            async for row in conn.cursor(
                "SELECT ((properties)::text)::jsonb AS props, "
                "start_id::text AS s, end_id::text AS d "
                f'FROM ONLY "{tenant}"."{edge_type}";',
                prefetch=_FETCH,
            ):
                c.add(json.loads(row["props"]), None, f"{row['s']} -> {row['d']}")
            collectors.append(c)
            edge_ends.update(
                gid for refs in c.example_refs.values() for ref in refs for gid in ref.split(" -> ")
            )
        names = await _external_ids(conn, tenant, edge_ends)
    results = [c.result(names) for c in collectors]
    return PropertiesCheckResponse(
        tenant=tenant,
        mode=spec.property_schema.get("mode", "off"),
        rows=sum(r.rows for r in results),
        rows_with_errors=sum(r.rows_with_errors for r in results),
        rows_with_warnings=sum(r.rows_with_warnings for r in results),
        labels=results,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def _external_ids(conn: asyncpg.Connection, tenant: str, gids: set[str]) -> dict[str, str]:
    """The external ids of nodes by graphid (to name edge examples by their ends)."""
    if not gids:
        return {}
    rows = await conn.fetch(
        "SELECT id::text AS gid, ((properties)::text)::jsonb ->> 'id' AS ext "
        f'FROM "{tenant}"."_ag_label_vertex" WHERE id::text = ANY($1::text[]);',
        sorted(gids),
    )
    return {r["gid"]: r["ext"] for r in rows}
