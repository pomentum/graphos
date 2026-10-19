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
  names graphos computes itself, once.

Caps keep it short (40 labels, 25 properties a label, 60 label pairs, 20
values a property, values cut at 80 characters); each list says how many
there were, so a cut is visible.  Labels above 50,000 nodes are read on a
sample, flagged ``estimated``.  It is kept per tenant for
five minutes (:data:`TTL_S`) and dropped when the schema changes.
``format=text`` answers the compact text alone, for a prompt.
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any

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
    SummaryFormat,
    SummaryLabel,
    SummaryLink,
    SummaryProperty,
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
SAMPLE_ROWS = 50_000
# How long a tenant's summary is kept; a schema change drops it at once.
TTL_S = 300

_cache: dict[str, tuple[float, str, SchemaSummaryResponse]] = {}


def _fingerprint(spec: TenantSpec) -> str:
    return json.dumps(
        [sorted(spec.vertex_labels), sorted(spec.edge_labels), sorted(spec.label_hierarchy),
         spec.property_schema, spec.search_indexes],
        sort_keys=True, default=str,
    )


async def _label_properties(
    conn: asyncpg.Connection, tenant: str, label: str, rows: int,
) -> tuple[list[tuple[str, int, int]], dict[str, list[tuple[str, int]]], float]:
    """``(key, records, distinct)`` for the label's own rows, the values of the
    keys with few of them, and the sampling scale (1 when every row was read)."""
    sampled = rows > SAMPLE_ROWS
    pct = SAMPLE_ROWS * 100.0 / rows if sampled else 100.0
    sample = f" TABLESAMPLE SYSTEM ({pct:.4f}) REPEATABLE (7)" if sampled else ""
    scale = 100.0 / pct
    source = (
        f'(SELECT properties::text::jsonb AS p FROM ONLY "{tenant}"."{label}"{sample}) s, '
        f"jsonb_each_text(s.p) AS e(k, v)"
    )
    stats = await conn.fetch(
        f"SELECT e.k, count(*) AS records, count(DISTINCT e.v) AS n_distinct FROM {source} "
        f"WHERE e.k <> 'id' GROUP BY e.k ORDER BY count(*) DESC, e.k;",
    )
    few = [
        r["k"] for r in stats
        if r["n_distinct"] <= MAX_VALUES and not reserved.is_reserved(r["k"])
    ]
    values: dict[str, list[tuple[str, int]]] = {}
    if few:
        for r in await conn.fetch(
            f"SELECT e.k, e.v, count(*) AS n FROM {source} WHERE e.k = ANY($1::text[]) "
            f"GROUP BY e.k, e.v ORDER BY e.k, count(*) DESC, e.v;",
            few,
        ):
            values.setdefault(r["k"], []).append((str(r["v"]), round(r["n"] * scale)))
    return [(r["k"], round(r["records"] * scale), r["n_distinct"]) for r in stats], values, scale


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
                ", ".join(f"{v.value!r} {v.count:,}" for v in p.values) if p.values
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
    lines.append(
        "Use label, type, property names and values exactly as written here; look a "
        "property's values up (get_property_values) before filtering on one.",
    )
    return "\n".join(lines)


async def build_summary(
    request: Request, tenant: str, principal: Any,
) -> SchemaSummaryResponse:
    """The summary, from the cache when it is fresh for the current schema."""
    started = time.perf_counter()
    spec = registered_tenants()[tenant]
    key = _fingerprint(spec)
    hit = _cache.get(tenant)
    if hit is not None and hit[1] == key and time.monotonic() - hit[0] < TTL_S:
        return hit[2].model_copy(update={
            "cached": True, "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

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
                    values=[ValueCount(value=v[:MAX_VALUE_CHARS], count=n) for v, n in vals]
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
    indexes = [
        SummarySearchIndex(
            name=name, labels=list(body.get("labels") or ["*"]),
            properties=list(body.get("properties") or []),
            analyzer=str(body.get("analyzer") or "identifier"),
        )
        for name, body in sorted(spec.search_indexes.items())
    ]
    summary = SchemaSummaryResponse(
        tenant=tenant, labels=labels, labels_total=len(the_map.labels),
        links=[
            SummaryLink(src_label=x.src_label, type=x.type, dst_label=x.dst_label, count=x.count)
            for x in the_map.links[:MAX_LINKS]
        ],
        links_total=len(the_map.links), search_indexes=indexes, saved_queries=saved_queries,
        computed=sorted(computed), estimated=estimated, text="", cached=False, elapsed_ms=0,
    )
    summary = summary.model_copy(update={
        "text": _render(summary), "elapsed_ms": int((time.perf_counter() - started) * 1000),
    })
    _cache[tenant] = (time.monotonic(), key, summary)
    return summary


@router.get(
    "/{tenant}/schema/summary",
    response_model=SchemaSummaryResponse,
    summary="The tenant's schema as a model needs it: names, counts and values",
    description=(
        "Labels with their node counts, parent and properties (the declared type, `required` "
        "and enum, the rows that hold each property, its distinct values and, when there are "
        "at most 20, every value with its count), the label pairs per edge type with their "
        "counts, the search indexes, the saved queries with their parameters, and the names "
        "graphos computes itself. Compact on purpose (about 1,000 tokens on a mid-sized "
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
