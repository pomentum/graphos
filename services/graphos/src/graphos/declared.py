"""The DDL behind a tenant's declared property indexes and generated columns.

A registration declares them (``contracts.tenants.PropertyIndexSpec``,
``GeneratedColumnSpec``) and graphos writes the SQL here, from names the
contracts already validated.  An index uses the same expression as the reads
it serves (``search.value_sql``, the one ``where`` filters compile to), so the
planner can use it.

Before 0.6.0 registrations carried the SQL itself (``indexes``, and
``generated_columns`` as statements).  :func:`convert_legacy` turns a stored
spec's statements into declarations where one says the same thing; the rest
are left out of the spec, while what they created stays in Postgres.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from graphos.search import value_sql

# ── Property indexes ────────────────────────────────────────────────────────

_KIND_SUFFIX = {"equality": "btree", "trigram": "trgm"}
_PG_NAME_MAX = 63


def index_name(idx: dict[str, Any]) -> str:
    """The index's own ``name``, else ``<Label>_<properties>_<kind>_idx``."""
    if idx.get("name"):
        return str(idx["name"])
    kind = "unique" if idx.get("unique") else _KIND_SUFFIX[str(idx.get("kind", "equality"))]
    name = f"{idx['label']}_{'_'.join(idx['properties'])}_{kind}_idx"
    if len(name) <= _PG_NAME_MAX:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]  # noqa: S324 — a name, not security
    return f"{name[:40]}_{digest}_idx"


def index_definition(idx: dict[str, Any]) -> str:
    """What follows ``ON <table>`` in the index's ``CREATE INDEX``."""
    if idx.get("kind") == "trigram":
        return f"USING gin (({value_sql(idx['properties'][0])}) gin_trgm_ops)"
    return "USING btree (" + ", ".join(f"({value_sql(p)})" for p in idx["properties"]) + ")"


def describe_index(idx: dict[str, Any]) -> str:
    """``Document(kind)``, ``User(email) unique``, ``Doc(title) trigram``."""
    text = f"{idx['label']}({', '.join(idx['properties'])})"
    if idx.get("unique"):
        text += " unique"
    if idx.get("kind") == "trigram":
        text += " trigram"
    return text


# ── Generated columns ───────────────────────────────────────────────────────


def column_expression(col: dict[str, Any]) -> str:
    """The column's expression.  Immutable, and never raises on a write:
    ``numeric`` / ``boolean`` read the value only when its JSON type is that."""
    doc = "((properties)::text)::jsonb"
    prop = col["property"]
    kind = col.get("type", "text")
    if kind == "jsonb":
        return f"({doc} -> '{prop}')"
    if kind in ("numeric", "boolean"):
        json_type = "number" if kind == "numeric" else "boolean"
        return (
            f"CASE WHEN jsonb_typeof({doc} -> '{prop}') = '{json_type}' "
            f"THEN ({value_sql(prop)})::{kind} END"
        )
    return value_sql(prop)


def column_definition(col: dict[str, Any]) -> str:
    """What follows ``ADD COLUMN <name>``."""
    return f"{col.get('type', 'text')} GENERATED ALWAYS AS ({column_expression(col)}) STORED"


def describe_column(col: dict[str, Any]) -> str:
    """``Document.kind (text, from kind)``."""
    return f"{col['label']}.{col['column']} ({col.get('type', 'text')}, from {col['property']})"


# ── Statements stored before 0.6.0 ──────────────────────────────────────────
#
# They were validated when registered, so they are single statements on the
# tenant's own tables; here they are only read, never run.

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
_LEGACY_INDEX = re.compile(
    rf"^CREATE (?P<unique>UNIQUE )?INDEX IF NOT EXISTS (?P<name>{_IDENT}) "
    rf"ON (?:ONLY )?{_IDENT} ?\. ?(?P<table>{_IDENT}) ?"
    r"(?:USING (?P<method>\w+) ?)?\((?P<columns>.*)\)$",
    re.IGNORECASE,
)
_LEGACY_COLUMN = re.compile(
    rf"^ALTER TABLE (?:ONLY )?{_IDENT} ?\. ?(?P<table>{_IDENT}) "
    rf"ADD (?:COLUMN )?(?:IF NOT EXISTS )?(?P<column>{_IDENT}) (?P<type>\w+) "
    r"GENERATED ALWAYS AS ?\((?P<expr>.*)\) STORED$",
    re.IGNORECASE,
)
_PROPERTY = r"'(?P<prop>[A-Za-z_][A-Za-z0-9_]{0,62})'"
# A property's text (``->>``) or JSON (``->``) value, once whitespace and
# parentheses are removed: ``((properties::text)::jsonb ->> 'kind')``.
_TEXT_VALUE = re.compile(rf"^properties::text::jsonb->>{_PROPERTY}$", re.IGNORECASE)
_JSON_VALUE = re.compile(rf"^properties::text::jsonb->{_PROPERTY}$", re.IGNORECASE)
_TRIGRAM_VALUE = re.compile(rf"^properties::text::jsonb->>{_PROPERTY}gin_trgm_ops$", re.IGNORECASE)
_COLUMN_TYPES = {"text": "text", "varchar": "text", "jsonb": "jsonb"}


def _bare(expr: str) -> str:
    return re.sub(r"[\s()]", "", expr)


def _unquote(ident: str) -> str:
    return ident[1:-1] if ident.startswith('"') else ident.lower()


def _top_level_split(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _legacy_index(stmt: str, labels: set[str]) -> dict[str, Any] | None:
    m = _LEGACY_INDEX.match(re.sub(r"\s+", " ", stmt.strip().rstrip(";")).strip())
    if m is None or _unquote(m["table"]) not in labels:
        return None
    method = (m["method"] or "btree").lower()
    columns = [_bare(c) for c in _top_level_split(m["columns"])]
    if method == "gin" and len(columns) == 1 and not m["unique"]:
        trigram = _TRIGRAM_VALUE.match(columns[0])
        if trigram is None:
            return None
        properties, kind = [trigram["prop"]], "trigram"
    elif method == "btree" and len(columns) <= 4:  # noqa: PLR2004 — PropertyIndexSpec max
        matches = [_TEXT_VALUE.match(c) for c in columns]
        if not all(matches):
            return None
        properties, kind = [x["prop"] for x in matches if x], "equality"
    else:
        return None
    if len(set(properties)) != len(properties):
        return None
    return {
        "label": _unquote(m["table"]), "properties": properties, "kind": kind,
        "unique": bool(m["unique"]), "name": _unquote(m["name"]),
    }


def _legacy_column(stmt: str, labels: set[str]) -> dict[str, Any] | None:
    m = _LEGACY_COLUMN.match(re.sub(r"\s+", " ", stmt.strip().rstrip(";")).strip())
    if m is None or _unquote(m["table"]) not in labels:
        return None
    kind = _COLUMN_TYPES.get(m["type"].lower())
    value = (_JSON_VALUE if kind == "jsonb" else _TEXT_VALUE).match(_bare(m["expr"]))
    column = _unquote(m["column"])
    if kind is None or value is None or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", column):
        return None
    return {"label": _unquote(m["table"]), "column": column, "property": value["prop"],
            "type": kind}


def has_legacy(payload: dict[str, Any]) -> bool:
    """A stored spec that still holds SQL statements."""
    return "indexes" in payload or any(
        isinstance(c, str) for c in payload.get("generated_columns") or ()
    )


def convert_legacy(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A stored spec with its statements turned into declarations where one
    says the same thing, and the statements left out (to log)."""
    labels = {str(x) for x in payload.get("vertex_labels") or ()}
    out = dict(payload)
    indexes = [dict(x) for x in payload.get("property_indexes") or () if isinstance(x, dict)]
    columns = [dict(x) for x in payload.get("generated_columns") or () if isinstance(x, dict)]
    dropped: list[str] = []
    for stmt in out.pop("indexes", None) or ():
        idx = _legacy_index(str(stmt), labels)
        if idx is None:
            dropped.append(str(stmt))
        elif idx not in indexes:
            indexes.append(idx)
    for stmt in payload.get("generated_columns") or ():
        if isinstance(stmt, dict):
            continue
        col = _legacy_column(str(stmt), labels)
        if col is None or any(
            (c["label"], c["column"]) == (col["label"], col["column"]) for c in columns
        ):
            dropped.append(str(stmt))
        else:
            columns.append(col)
    out["property_indexes"] = indexes
    out["generated_columns"] = columns
    return out, dropped
