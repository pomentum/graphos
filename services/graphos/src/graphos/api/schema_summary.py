"""``GET /v1/{tenant}/schema/summary``: the schema a model needs before querying.

ADR-0002 §1.  An agent that writes queries against a graph it has not seen
guesses label, type, property names and, worst, values: ``status =
"active"`` against rows that say ``ACT`` returns nothing and reads as a fact.
The summary gives it, compactly, what the data really holds:

- labels with their node counts (``graph/map``), their parent, and their
  properties: the declared type, ``required`` and enum where the property
  definitions say, and for every property the rows that hold it, its
  distinct values, and, when there are at most 20, every value with its
  count (B4);
- the label pairs per edge type with their edge counts;
- the search indexes, the saved queries with their typed parameters, and the
  names graphos computes itself, once;
- the deprecated names with what replaces them, and the latest renames
  (ADR-0009), so a model neither writes a name on its way out nor one that
  is gone.

Caps keep it short (40 labels, 25 properties a label, 60 label pairs, 20
values a property, values cut at 80 characters); each list says how many
there were, so a cut is visible.  Labels above 50,000 nodes are read on a
sample, flagged ``estimated``.  It is kept per tenant for
five minutes (:data:`TTL_S`) and dropped when the schema changes.
``format=text`` answers the compact text alone, for a prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any, cast

import asyncpg
from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from graphos import properties, reserved
from graphos.api.graph import _check_tenant
from graphos.api.graph_map import graph_map
from graphos.auth import ReadPrincipal, Tenant
from graphos.bootstrap import TenantSpec, registered_tenants
from graphos.contracts.schema_summary import (
    SchemaSummaryResponse,
    SummaryDeprecated,
    SummaryFormat,
    SummaryLabel,
    SummaryLink,
    SummaryProperty,
    SummaryRename,
    SummarySavedQuery,
    SummarySearchIndex,
    ValueCount,
)

router = APIRouter(tags=["tenants"])

MAX_LABELS = 40
MAX_PROPERTIES = 25
MAX_LINKS = 60
MAX_VALUES = 20
MAX_VALUE_CHARS = 80
MAX_RENAMES = 20
SAMPLE_ROWS = 50_000
# How long a tenant's summary is kept; a schema change drops it at once.
TTL_S = 300

_cache: dict[str, tuple[float, str, SchemaSummaryResponse]] = {}
# One rebuild per tenant at a time: a cold summary scans every label.
_building: dict[str, asyncio.Lock] = {}


async def _fingerprint(conn: asyncpg.Connection, tenant: str, spec: TenantSpec) -> str:
    """What the summary depends on besides the data: the schema, the saved
    queries, and the graph itself (a tenant deleted and created again under the
    same name is another graph)."""
    row = await conn.fetchrow(
        "SELECT (SELECT graphid::text FROM ag_catalog.ag_graph WHERE name = $1) AS graph, "
        "(SELECT count(*)::text || '/' || COALESCE(max(updated_at)::text, '') "
        " FROM graphos_meta.saved_queries WHERE tenant = $1) AS saved;",
        tenant,
    )
    return json.dumps(
        [sorted(spec.vertex_labels), sorted(spec.edge_labels), sorted(spec.label_hierarchy),
         spec.property_schema, spec.search_indexes, spec.deprecated, spec.renames,
         row["graph"], row["saved"]],
        sort_keys=True, default=str,
    )


async def _label_properties(
    conn: asyncpg.Connection, tenant: str, label: str, rows: int,
) -> tuple[list[tuple[str, int, int]], dict[str, list[tuple[str, str, int]]], float]:
    """``(key, records, distinct)`` for the label's own rows, the values of the
    keys with few of them, and the sampling scale (1 when every row was read)."""
    sampled = rows > SAMPLE_ROWS
    pct = SAMPLE_ROWS * 100.0 / rows if sampled else 100.0
    sample = f" TABLESAMPLE SYSTEM ({pct:.4f}) REPEATABLE (7)" if sampled else ""
    scale = 100.0 / pct
    source = (
        f'(SELECT properties::text::jsonb AS p FROM ONLY "{tenant}"."{label}"{sample}) s, '
        f"jsonb_each(s.p) AS e(k, v)"
    )
    stats = await conn.fetch(
        f"SELECT e.k, count(*) AS records, count(DISTINCT e.v) AS n_distinct FROM {source} "
        f"WHERE e.k <> 'id' GROUP BY e.k ORDER BY count(*) DESC, e.k;",
    )
    few = [
        r["k"] for r in stats
        if r["n_distinct"] <= MAX_VALUES and not reserved.is_reserved(r["k"])
    ]
    values: dict[str, list[tuple[str, str, int]]] = {}
    if few:
        # A string as it is; any other value as its JSON text (5, true, null).
        for r in await conn.fetch(
            f"SELECT e.k, CASE WHEN jsonb_typeof(e.v) = 'string' THEN e.v #>> '{{}}' "
            f"ELSE e.v::text END AS v, jsonb_typeof(e.v) AS kind, count(*) AS n "
            f"FROM {source} WHERE e.k = ANY($1::text[]) "
            f"GROUP BY e.k, e.v ORDER BY e.k, count(*) DESC, 2;",
            few,
        ):
            values.setdefault(r["k"], []).append((r["v"], r["kind"], round(r["n"] * scale)))
    return [(r["k"], round(r["records"] * scale), r["n_distinct"]) for r in stats], values, scale


def _shown(v: ValueCount) -> str:
    """A value as the text form shows it: a string quoted, any other as JSON."""
    return json.dumps(v.value, ensure_ascii=False) if v.kind == "string" else v.value


def _cut(text: str) -> str:
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def _deprecated(spec: TenantSpec) -> list[SummaryDeprecated]:
    out: list[SummaryDeprecated] = []
    for part, kind in (("vertex", "label"), ("edge", "edge_type")):
        for name, entry in sorted((spec.deprecated.get(part) or {}).items()):
            if entry.get("deprecated"):
                out.append(SummaryDeprecated(
                    kind=kind, name=name, replaced_by=entry.get("replaced_by"),
                    note=entry.get("note"),
                ))
            props = cast("dict[str, Any]", entry.get("properties") or {})
            for prop, about in sorted(props.items()):
                out.append(SummaryDeprecated(
                    kind="property", name=prop, on=name, replaced_by=about.get("replaced_by"),
                    note=about.get("note"),
                ))
    return out


def _renames(spec: TenantSpec) -> list[SummaryRename]:
    return [
        SummaryRename(
            kind=r["kind"], from_=r["from"], to=r["to"], on=r.get("on"),
            at=str(r["at"]) if r.get("at") else None, status=str(r.get("status") or "done"),
        )
        for r in list(reversed(spec.renames))[:MAX_RENAMES]
    ]


def _named(kind: str, name: str, on: str | None) -> str:
    return f"property {on}.{name}" if kind == "property" else (
        f"label {name}" if kind == "label" else f"edge type {name}")


def _render(s: SchemaSummaryResponse) -> str:
    """The compact text form, for a prompt."""
    lines = [
        f"graphos tenant {s.tenant}: {s.labels_total} labels with nodes, "
        f"{s.links_total} label pairs with edges"
        + (" (some counts estimated from samples)" if s.estimated else "") + ".",
        "Labels (node count; property: type, rows, distinct values or the values):",
    ]
    for label in s.labels:
        head = f"- {label.label} ({label.count:,}" + (
            f"; child of {label.parent})" if label.parent else ")")
        props = []
        for p in label.properties:
            kind = (p.type or "?") + (" required" if p.required else "")
            seen = (
                ", ".join(f"{_shown(v)} {v.count:,}" for v in p.values) if p.values
                else f"{p.distinct:,} distinct"
            )
            props.append(f"{p.name}: {kind}, {p.records:,} rows, {seen}")
        more = label.properties_total - len(label.properties)
        lines.append(head + (": " + "; ".join(props) if props else "")
                     + (f"; and {more} more properties" if more > 0 else ""))
    if s.labels_total > len(s.labels):
        lines.append(f"- and {s.labels_total - len(s.labels)} more labels")
    lines.append("Relationships ((source)-[:TYPE]->(target) edge count):")
    lines += [f"- ({x.src_label})-[:{x.type}]->({x.dst_label}) {x.count:,}" for x in s.links]
    if s.links_total > len(s.links):
        lines.append(f"- and {s.links_total - len(s.links)} more label pairs")
    if s.search_indexes:
        lines.append("Search indexes (graph/search?index=): " + "; ".join(
            f"{i.name} ({i.analyzer}) on {', '.join(i.labels)}: {', '.join(i.properties)}"
            for i in s.search_indexes))
    if s.saved_queries:
        lines.append("Saved queries: " + "; ".join(
            f"{q.name}({', '.join(f'{k}: {t}' for k, t in q.params.items())})"
            + (f" - {q.description}" if q.description else "") for q in s.saved_queries))
    if s.computed:
        lines.append("Computed by graphos (read-only): " + ", ".join(s.computed))
    if s.deprecated:
        lines.append("Deprecated (still readable; do not use in new writes): " + "; ".join(
            _named(d.kind, d.name, d.on)
            + (f", use {d.replaced_by}" if d.replaced_by else "")
            + (f" ({d.note})" if d.note else "") for d in s.deprecated))
    if s.renames:
        lines.append("Renamed (the old names are gone): " + "; ".join(
            f"{_named(r.kind, r.from_, r.on)} -> {r.to}"
            + (f" ({r.at[:10]})" if r.at and r.status == "done" else "")
            + (f" ({r.status})" if r.status != "done" else "") for r in s.renames))
    lines.append(
        "Use label, type, property names and values exactly as written here (strings are "
        "quoted, other values are JSON; a value ending with … was cut); look a property's "
        "values up (get_property_values) before filtering on one.",
    )
    return "\n".join(lines)


async def build_summary(
    request: Request, tenant: str, principal: Any,
) -> SchemaSummaryResponse:
    """The summary, from the cache when it is fresh for the current schema."""
    started = time.perf_counter()
    spec = registered_tenants()[tenant]
    async with request.app.state.pool.acquire() as conn:
        key = await _fingerprint(conn, tenant, spec)

    def fresh() -> SchemaSummaryResponse | None:
        hit = _cache.get(tenant)
        if hit is None or hit[1] != key or time.monotonic() - hit[0] >= TTL_S:
            return None
        return hit[2].model_copy(update={
            "cached": True, "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    cached = fresh()
    if cached is not None:
        return cached
    async with _building.setdefault(tenant, asyncio.Lock()):
        cached = fresh()        # built by another request while this one waited
        if cached is not None:
            return cached
        summary = await _build(request, tenant, principal, spec, started)
        _cache[tenant] = (time.monotonic(), key, summary)
        return summary


async def _build(
    request: Request, tenant: str, principal: Any, spec: TenantSpec, started: float,
) -> SchemaSummaryResponse:
    the_map = await graph_map(tenant=tenant, request=request, principal=principal, exact=False)
    policies = properties.policies_for(spec)
    parents = dict(spec.label_hierarchy)
    computed: set[str] = set()
    labels: list[SummaryLabel] = []
    estimated = the_map.estimated
    async with request.app.state.pool.acquire() as conn:
        for entry in the_map.labels[:MAX_LABELS]:
            stats, values, scale = await _label_properties(conn, tenant, entry.label, entry.count)
            estimated = estimated or scale != 1.0
            declared = policies.for_vertex(entry.label).properties
            props: list[SummaryProperty] = []
            for name, records, n_distinct in stats:
                if reserved.is_reserved(name):
                    computed.add(name)
                    continue
                spec_of = declared[name].spec if name in declared else {}
                vals = values.get(name)
                props.append(SummaryProperty(
                    name=name, type=spec_of.get("type"), required=bool(spec_of.get("required")),
                    enum=spec_of.get("enum"), records=records, distinct=n_distinct,
                    values=[ValueCount(value=_cut(v), kind=k, count=n) for v, k, n in vals]
                    if vals else None,
                    estimated=scale != 1.0,
                ))
            labels.append(SummaryLabel(
                label=entry.label, count=entry.count, parent=parents.get(entry.label),
                properties=props[:MAX_PROPERTIES], properties_total=len(props),
            ))
        saved = await conn.fetch(
            "SELECT name, definition::text AS d FROM graphos_meta.saved_queries "
            "WHERE tenant = $1 ORDER BY name;",
            tenant,
        )
    saved_queries = []
    for row in saved:
        d = json.loads(row["d"])
        saved_queries.append(SummarySavedQuery(
            name=row["name"], description=str(d.get("description") or ""),
            params={
                k: str((v or {}).get("type", "string"))
                for k, v in (d.get("params") or {}).items()
            },
        ))
    indexes = []
    for name, body in sorted(spec.search_indexes.items()):
        index = cast("dict[str, Any]", body)
        indexes.append(SummarySearchIndex(
            name=name, labels=[str(x) for x in index.get("labels") or ["*"]],
            properties=[str(x) for x in index.get("properties") or []],
            analyzer=str(index.get("analyzer") or "identifier"),
        ))
    summary = SchemaSummaryResponse(
        tenant=tenant, labels=labels, labels_total=len(the_map.labels),
        links=[
            SummaryLink(src_label=x.src_label, type=x.type, dst_label=x.dst_label, count=x.count)
            for x in the_map.links[:MAX_LINKS]
        ],
        links_total=len(the_map.links), search_indexes=indexes, saved_queries=saved_queries,
        computed=sorted(computed), deprecated=_deprecated(spec), renames=_renames(spec),
        estimated=estimated, text="", cached=False, elapsed_ms=0,
    )
    return summary.model_copy(update={
        "text": _render(summary), "elapsed_ms": int((time.perf_counter() - started) * 1000),
    })


@router.get(
    "/{tenant}/schema/summary",
    response_model=SchemaSummaryResponse,
    summary="The tenant's schema as a model needs it: names, counts and values",
    description=(
        "Labels with their node counts, parent and properties (the declared type, `required` "
        "and enum, the rows that hold each property, its distinct values and, when there are "
        "at most 20, every value with its count), the label pairs per edge type with their "
        "counts, the search indexes, the saved queries with their parameters, the names "
        "graphos computes itself, the deprecated names with what replaces them and the "
        "latest renames. Compact on purpose (about 1,000 tokens on a mid-sized "
        "tenant): capped lists say how many there were. `text` is the same as compact text; "
        "`format=text` answers it alone (`text/plain`). Kept per tenant for "
        "five minutes and dropped when the schema changes."
    ),
    responses={200: {
        "description": "The summary, as JSON or (`format=text`) as text.",
        "content": {"text/plain": {"schema": {"type": "string"}}},
    }},
)
async def schema_summary(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    format: Annotated[  # noqa: A002 — the query parameter's name
        SummaryFormat, Query(description="`json` (the default) or `text`."),
    ] = "json",
) -> SchemaSummaryResponse | PlainTextResponse:
    _check_tenant(tenant, principal)
    summary = await build_summary(request, tenant, principal)
    if format == "text":
        return PlainTextResponse(summary.text)
    return summary
