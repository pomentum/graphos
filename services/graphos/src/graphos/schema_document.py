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
from typing import Any, cast

import asyncpg

from graphos import __version__, by_entry, declared
from graphos.bootstrap import DEFAULT_COMMUNITY_ALGORITHM, TenantSpec
from graphos.search import value_sql

FORMAT = "graphos.schema"
# Version 2 when the document declares properties (``property_schema``), 3
# when it declares deprecations: a server that does not know them refuses the
# file rather than dropping them.
VERSION = 4

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
    doc: dict[str, Any] = {
        "_export": {
            "format": FORMAT,
            "version": 1,
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
        # Cosmetic: a server that predates it ignores the key (the version stays 1).
        "display": {
            part: dict(sorted(spec.display[part].items()))
            for part in ("vertex", "edge") if spec.display.get(part)
        },
    }
    if spec.property_schema:
        doc["_export"]["version"] = 2
        doc["property_schema"] = _sorted_schema(spec.property_schema)
    if spec.deprecated:
        # A server before 3 refuses the key: say so in the version.
        doc["_export"]["version"] = 3
        doc["deprecated"] = {
            part: dict(sorted(spec.deprecated[part].items()))
            for part in ("vertex", "edge") if spec.deprecated.get(part)
        }
    if (spec.community_algorithm or DEFAULT_COMMUNITY_ALGORITHM) != DEFAULT_COMMUNITY_ALGORITHM:
        # Only when not the default, so other exports stay as they were; a
        # server before 4 refuses the key: say so in the version.
        doc["_export"]["version"] = 4
        doc["community_algorithm"] = spec.community_algorithm
    return doc


def _sorted_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A property schema with every map sorted, so exports compare line by line."""
    out: dict[str, Any] = {}
    if schema.get("mode"):
        out["mode"] = schema["mode"]
    for part in ("vertex", "edge"):
        entries = schema.get(part) or {}
        if entries:
            out[part] = {
                name: {
                    **{k: v for k, v in sorted(entry.items()) if k != "properties"},
                    **({"properties": dict(sorted(entry["properties"].items()))}
                       if entry.get("properties") else {}),
                }
                for name, entry in sorted(entries.items())
            }
    return out


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
    display_removed: Iterable[tuple[str, str]] = (),
    schema_removed: Iterable[tuple[str, str]] = (),
    deprecated_removed: Iterable[tuple[str, str]] = (),
) -> TenantSpec:
    """The spec registering ``incoming`` would leave, as the server stores it.

    Merge (the default): labels, declarations, hierarchy, rules and declared
    pairs are added to the stored ones, ``meta`` merged key by key (the keys
    in ``meta_removed``, sent as ``null``, removed), search indexes replaced
    by name, display entries replaced by entry (those in ``display_removed``
    removed), and deprecations likewise.  ``replace`` (``force_shrink``): the incoming
    spec, but declared pairs only when the request states them.  Either way
    ``strict_endpoints`` stays as stored when the request leaves it out, and
    the ``renames`` history as stored.
    """
    if previous is None:
        return dc_replace(
            incoming, strict_endpoints=bool(incoming.strict_endpoints),
            community_algorithm=incoming.community_algorithm or DEFAULT_COMMUNITY_ALGORITHM,
        )
    strict = (
        previous.strict_endpoints if incoming.strict_endpoints is None
        else incoming.strict_endpoints
    )
    algorithm = (
        incoming.community_algorithm or previous.community_algorithm
        or DEFAULT_COMMUNITY_ALGORITHM
    )
    if replace:
        endpoints = incoming.edge_endpoints if endpoints_stated else previous.edge_endpoints
        return dc_replace(
            incoming, edge_endpoints=endpoints, strict_endpoints=bool(strict),
            renames=previous.renames, community_algorithm=algorithm,
        )
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
        display=by_entry.merge(previous.display, incoming.display, tuple(display_removed)),
        property_schema=merged_property_schema(
            previous.property_schema, incoming.property_schema, tuple(schema_removed),
        ),
        deprecated=by_entry.merge(
            previous.deprecated, incoming.deprecated, tuple(deprecated_removed),
        ),
        renames=previous.renames,
        edge_endpoints=_union(previous.edge_endpoints, incoming.edge_endpoints),
        strict_endpoints=bool(strict),
        community_algorithm=algorithm,
    )


def merged_property_schema(
    stored: dict[str, Any], incoming: dict[str, Any], removed: by_entry.Removed,
) -> dict[str, Any]:
    """The property schema a merge registration leaves: entries merged by label or
    type, the mode replaced only when the request states one."""
    mode = incoming.get("mode") or stored.get("mode")
    return {**({"mode": mode} if mode else {}), **by_entry.merge(stored, incoming, removed)}


# ── What applying it would cost ─────────────────────────────────────────────

# The web UI's canvas backgrounds (light, dark); a declared colour too close to
# either is hard to see there.
_CANVASES = {"light": "#E3E5E9", "dark": "#26292E"}
_MIN_CONTRAST = 1.6


def _luminance(color: str) -> float:
    def channel(i: int) -> float:
        c = int(color[i:i + 2], 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: PLR2004 - sRGB
    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio of two ``#RRGGBB`` colours (1 to 21)."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hard_to_see(previous: TenantSpec | None, after: TenantSpec) -> list[str]:
    """Declared colours, new or changed, too close to a canvas background."""
    out: list[str] = []
    before = previous.display if previous else {}
    for part in ("vertex", "edge"):
        for name, entry in after.display.get(part, {}).items():
            color = entry.get("color")
            was = before.get(part, {}).get(name, {}).get("color")
            if not isinstance(color, str) or was == color:
                continue
            faint = [
                scheme for scheme, canvas in _CANVASES.items()
                if contrast(color, canvas) < _MIN_CONTRAST
            ]
            if faint:
                out.append(
                    f"display colour {color} of {name!r} is hard to see on the "
                    f"{' and '.join(faint)} canvas"
                )
    return out


async def row_estimate(conn: asyncpg.Connection, tenant: str, label: str) -> int | None:
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
    out = _hard_to_see(previous, after)
    if previous is None:
        return out
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
            rows = await row_estimate(conn, after.name, str(col["label"]))
            if rows:
                out.append(
                    f"generated column {col['label']}.{col['column']} is added to a label with "
                    f"about {rows:,} nodes: its table is rewritten, writes to it wait meanwhile"
                )
        for idx in after.property_indexes:
            if idx in previous.property_indexes or not idx.get("unique"):
                continue
            label = str(idx["label"])
            if not await row_estimate(conn, after.name, label):
                continue
            props = cast("Iterable[object]", idx["properties"])
            values = ", ".join(value_sql(str(p)) for p in props)
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
            labels = cast("list[str]", body.get("labels") or ["*"])
            if "*" in labels:
                labels = list(after.vertex_labels)
            rows = sum([await row_estimate(conn, after.name, str(lab)) or 0 for lab in labels])
            if rows > _LARGE_LABEL_ROWS:
                out.append(
                    f"search index {name!r} covers about {rows:,} nodes: it is built without "
                    "blocking writes, which can take minutes"
                )
    return out
