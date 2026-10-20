"""Renamed and deprecated names of a tenant's schema (ADR-0009).

A tenant's spec keeps every rename, oldest first, in ``renames``: ``{kind,
from, to, on, at, by, status, job_id}``, ``kind`` one of ``label``,
``edge_type`` and ``property`` (``on``: the label or type a property was
renamed on).  A name renamed away is never a silent unknown: a write naming
it answers 409 with where it went, and a registration that would create it
again is refused the same way.

``deprecated`` (per vertex label and edge type, merged by entry as
``display`` is) marks names on their way out; the property checks turn it
into warnings, in every mode (``graphos.properties``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import HTTPException, status

KINDS = ("label", "edge_type", "property")
_NAMED = ("label", "edge_type")


def renamed_away(
    spec: Any, name: str, kinds: Iterable[str] = _NAMED,
) -> Mapping[str, Any] | None:
    """The latest rename of the label or edge type ``name``, when ``name`` is
    not a label or type of the tenant again since; else None."""
    if name in spec.vertex_labels or name in spec.edge_labels:
        return None
    wanted = set(kinds)
    entry: Mapping[str, Any]
    for entry in reversed(spec.renames):
        if entry.get("kind") in wanted and entry.get("from") == name:
            return entry
    return None


def where_it_went(entry: Mapping[str, Any]) -> str:
    """``"X" was renamed to "Y" on <date>``."""
    when = str(entry.get("at") or "")[:10]
    on = f" on {entry['on']}" if entry.get("on") else ""
    return f"{entry.get('from')!r}{on} was renamed to {entry.get('to')!r}" + (
        f" on {when}" if when else ""
    )


def refuse_renamed(spec: Any, name: str | None, *, kinds: Iterable[str] = _NAMED) -> None:
    """409 when ``name`` is a label or type renamed away (``None``: nothing to check)."""
    if name is None:
        return
    entry = renamed_away(spec, name, kinds)
    if entry is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{where_it_went(entry)}: use the new name",
        )


def why_unknown(spec: Any, name: str) -> str | None:
    """Why a label or type the tenant lacks is unknown, when it was renamed away."""
    entry = renamed_away(spec, name)
    return None if entry is None else f"{where_it_went(entry)}: use the new name"


def renamed_properties(spec: Any, on: str) -> dict[str, Mapping[str, Any]]:
    """The properties renamed on the label or type ``on``: old name -> its latest rename."""
    out: dict[str, Mapping[str, Any]] = {}
    for entry in spec.renames:
        if entry.get("kind") == "property" and entry.get("on") == on:
            out[str(entry["from"])] = entry
    return out


def deprecated_entry(spec: Any, part: str, name: str) -> Mapping[str, Any]:
    """The ``deprecated`` entry of a vertex label (``part="vertex"``) or edge type."""
    found: Mapping[str, Any] = (spec.deprecated.get(part) or {}).get(name) or {}
    return found


def deprecation_note(entry: Mapping[str, Any], what: str) -> str:
    """``deprecated, use "X": <note>``, from a deprecated label, type or property entry."""
    out = f"{what} is deprecated"
    if entry.get("replaced_by"):
        out += f", use {entry['replaced_by']!r}"
    if entry.get("note"):
        out += f": {entry['note']}"
    return out
