"""A tenant's schema as a file: export it, and preview what importing one changes.

The export **is a registration body**: posting it to ``POST /v1/tenants``
registers that schema.  One extra top-level key, ``_export``, says what the
file is (format, version, the graphos version and tenant it came from, when);
a registration ignores it, and refuses a version it does not know.

The document is lossless (everything in the stored spec, plus the declared
pairs and ``strict_endpoints`` kept apart from it) and deterministic (lists
sorted, keys in a fixed order), so two exports of the same schema are equal
and a diff in git shows only what changed.

:func:`merged` computes, without touching the database, the spec a
registration would leave, with the same rules as ``bootstrap.persist_spec``
(merge) and ``persist_spec_replace`` (``force_shrink``): a dry run diffs it
against the stored spec, exactly as the change feed records the real import.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace as dc_replace
from datetime import datetime
from typing import Any

import asyncpg

from graphos import __version__, declared
from graphos.bootstrap import TenantSpec
from graphos.search import value_sql

FORMAT = "graphos.schema"
VERSION = 1

# A search index over more rows than this is worth a warning: it is built
# concurrently, which can take minutes.
_LARGE_LABEL_ROWS = 100_000


# ── Export ──────────────────────────────────────────────────────────────────


def _rule_key(rule: dict[str, Any]) -> str:
    return json.dumps(rule, sort_keys=True, default=str)


def document(spec: TenantSpec, *, now: datetime) -> dict[str, Any]:
    """The schema document of ``spec`` (a stored spec, endpoints included)."""
    rules: dict[str, list[dict[str, Any]]] = {}
    for edge_type, rule in spec.resolve_rules:
        rules.setdefault(edge_type, []).append(dict(rule))
    edge_labels: list[Any] = []
    for label in sorted(set(spec.edge_labels) | set(rules)):
        if label in rules:
            edge_labels.extend(
                {"name": label, "resolve_rule": rule}
                for rule in sorted(rules[label], key=_rule_key)
            )
        else:
            edge_labels.append(label)
    endpoints: dict[str, list[dict[str, str]]] = {}
    for edge_type, src, dst in sorted(set(spec.edge_endpoints)):
        endpoints.setdefault(edge_type, []).append({"src": src, "dst": dst})
    return {
        "_export": {
            "format": FORMAT,
            "version": VERSION,
            "graphos": __version__,
            "tenant": spec.name,
            "exported_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "name": spec.name,
        "vertex_labels": sorted(set(spec.vertex_labels)),
        "edge_labels": edge_labels,
        "label_hierarchy": dict(sorted(spec.label_hierarchy)),
        "edge_endpoints": endpoints,
        "strict_endpoints": bool(spec.strict_endpoints),
        "search_indexes": {
            name: dict(body) for name, body in sorted(spec.search_indexes.items())
        },
        "property_indexes": sorted(
            (dict(x) for x in spec.property_indexes),
            key=lambda x: (x["label"], x["properties"], x.get("kind", ""),
                           bool(x.get("unique")), x.get("name") or ""),
        ),
        "generated_columns": sorted(
            (dict(x) for x in spec.generated_columns),
            key=lambda x: (x["label"], x["column"]),
        ),
        "meta": dict(sorted(spec.meta.items())),
    }


def check_header(header: Any) -> None:
    """Refuse an ``_export`` header of another format or a newer version."""
    if not isinstance(header, dict):
        raise ValueError("`_export` must be an object")
    if header.get("format", FORMAT) != FORMAT:
        raise ValueError(f"`_export.format` is {header.get('format')!r}, not {FORMAT!r}")
    version = header.get("version", VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("`_export.version` must be a positive integer")
    if version > VERSION:
        raise ValueError(
            f"this schema file is version {version}; this server reads up to version "
            f"{VERSION} (it was exported by graphos {header.get('graphos', '?')})"
        )


# ── What a registration would leave ─────────────────────────────────────────


def _union(*parts: tuple[Any, ...]) -> tuple[Any, ...]:
    out: list[Any] = []
    for part in parts:
        for item in part:
            if item not in out:
                out.append(item)
    return tuple(out)


def merged(
    previous: TenantSpec | None,
    incoming: TenantSpec,
    *,
    replace: bool,
    endpoints_stated: bool,
    meta_removed: Iterable[str] = (),
) -> TenantSpec:
    """The spec registering ``incoming`` would leave, as the server stores it.

    Merge (the default): labels, declarations, hierarchy, rules and declared
    pairs are added to the stored ones, ``meta`` merged key by key (the keys
    in ``meta_removed``, sent as ``null``, removed), search indexes replaced
    by name.  ``replace`` (``force_shrink``): the incoming
    spec, but declared pairs only when the request states them.  Either way
    ``strict_endpoints`` stays as stored when the request leaves it out.
    """
    if previous is None:
        return dc_replace(incoming, strict_endpoints=bool(incoming.strict_endpoints))
    strict = (
        previous.strict_endpoints if incoming.strict_endpoints is None
        else incoming.strict_endpoints
    )
    if replace:
        endpoints = incoming.edge_endpoints if endpoints_stated else previous.edge_endpoints
        return dc_replace(incoming, edge_endpoints=endpoints, strict_endpoints=bool(strict))
    return TenantSpec(
        name=previous.name,
        vertex_labels=_union(previous.vertex_labels, incoming.vertex_labels),
        edge_labels=_union(previous.edge_labels, incoming.edge_labels),
        property_indexes=_union(previous.property_indexes, incoming.property_indexes),
        generated_columns=_union(previous.generated_columns, incoming.generated_columns),
        resolve_rules=_union(previous.resolve_rules, incoming.resolve_rules),
        label_hierarchy=_union(previous.label_hierarchy, incoming.label_hierarchy),
        meta={
            k: v for k, v in {**previous.meta, **incoming.meta}.items()
            if k not in set(meta_removed)
        },
        search_indexes={**previous.search_indexes, **incoming.search_indexes},
        edge_endpoints=_union(previous.edge_endpoints, incoming.edge_endpoints),
        strict_endpoints=bool(strict),
    )


# ── What applying it would cost ─────────────────────────────────────────────


async def _rows(conn: asyncpg.Connection, tenant: str, label: str) -> int | None:
    """Rows in a label table: ``None`` when it does not exist, else an estimate
    (``reltuples``), corrected to at least 1 when the table is not empty."""
    row = await conn.fetchrow(
        "SELECT c.reltuples::bigint AS estimate FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'r';",
        tenant, label,
    )
    if row is None:
        return None
    if row["estimate"] and row["estimate"] > 0:
        return int(row["estimate"])
    return int(bool(await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM "{tenant}"."{label}");')))


async def warnings(
    pool: asyncpg.Pool,
    previous: TenantSpec | None,
    after: TenantSpec,
    *,
    replace: bool,
) -> list[str]:
    """What applying the registration would cost or could still fail on.

    Cheap catalog reads, plus one duplicate check per new unique index.
    """
    if previous is None:
        return []
    out: list[str] = []
    if replace:
        gone = sorted(
            (set(previous.vertex_labels) - set(after.vertex_labels))
            | (set(previous.edge_labels) - set(after.edge_labels))
        )
        if gone:
            out.append(
                f"labels left out of the schema keep their tables and data: {', '.join(gone)}"
            )
    async with pool.acquire() as conn:
        for col in after.generated_columns:
            if col in previous.generated_columns:
                continue
            rows = await _rows(conn, after.name, str(col["label"]))
            if rows:
                out.append(
                    f"generated column {col['label']}.{col['column']} is added to a label with "
                    f"about {rows:,} nodes: its table is rewritten, writes to it wait meanwhile"
                )
        for idx in after.property_indexes:
            if idx in previous.property_indexes or not idx.get("unique"):
                continue
            label = str(idx["label"])
            if not await _rows(conn, after.name, label):
                continue
            values = ", ".join(value_sql(str(p)) for p in idx["properties"])
            if await conn.fetchval(
                f'SELECT EXISTS (SELECT 1 FROM "{after.name}"."{label}" '
                f"GROUP BY {values} HAVING count(*) > 1);",
            ):
                out.append(
                    f"unique property index {declared.describe_index(idx)}: nodes already share "
                    "these values, so registering it will fail (422)"
                )
        for name, body in after.search_indexes.items():
            if previous.search_indexes.get(name) == body:
                continue
            labels = body.get("labels") or ["*"]
            if "*" in labels:
                labels = list(after.vertex_labels)
            rows = sum([await _rows(conn, after.name, str(lab)) or 0 for lab in labels])
            if rows > _LARGE_LABEL_ROWS:
                out.append(
                    f"search index {name!r} covers about {rows:,} nodes: it is built without "
                    "blocking writes, which can take minutes"
                )
    return out
