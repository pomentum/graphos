"""Which parts of a re-registration break what consumers rely on.

A registration dry run (``POST /v1/tenants?dry_run=true``) lists every
change; this module picks out the ones that make something that worked
before stop working:

- **writes refused** that were accepted: a label or type left out of a
  replaced schema, ``strict_endpoints`` turned on, a declared pair removed
  while it is on, and a property rule that refuses more rows where the
  label or type is checked ``strict`` (the mode becoming strict, a property
  made required, a new required property, a type changed, an enum
  narrowed, a pattern, format or item rule changed, a minimum raised or a
  maximum lowered, undeclared keys rejected);
- **reads changed:** a search index or a label-hierarchy pair removed;
- **calls refused:** a resolve rule removed.

Relaxations (a rule dropped, an enum widened, ``warn`` instead of
``strict``), additions and ``recommended`` are never breaking: a warning
refuses nothing.  Existing rows are never rewritten by a registration, so a
tighter rule breaks the next write of a row, not the row; each entry
carries the label's or type's row estimate, and ``properties:check``
counts the rows that already break the rules.

Rules are compared as compiled (:func:`graphos.properties.policies_for`),
so ``*`` entries and the label hierarchy count as they do on writes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from graphos import properties
from graphos.bootstrap import TenantSpec

# A type change that only accepts more values.
_WIDENINGS = {("integer", "number")}

Part = Literal["vertex", "edge", "tenant"]


@dataclass(frozen=True, slots=True)
class Breaking:
    """One breaking change: where (``part``, ``name``), what (``change``) and why."""

    part: Part
    name: str | None     # the label or edge type; None for the tenant
    change: str          # a stable code, e.g. "property_required"
    detail: str          # one sentence for a person


def changes(previous: TenantSpec | None, after: TenantSpec, *, replace: bool) -> list[Breaking]:
    """The breaking changes from ``previous`` to ``after``; none for a new tenant."""
    if previous is None:
        return []
    out = list(_removals(previous, after, replace=replace))
    out.extend(_endpoint_changes(previous, after))
    out.extend(_rule_changes(previous, after))
    return out


def _removals(previous: TenantSpec, after: TenantSpec, *, replace: bool) -> Iterator[Breaking]:
    if replace:
        for label in sorted(set(previous.vertex_labels) - set(after.vertex_labels)):
            yield Breaking("vertex", label, "label_removed",
                           f"writes to {label} are refused; its nodes stay")
        for edge_type in sorted(set(previous.edge_labels) - set(after.edge_labels)):
            yield Breaking("edge", edge_type, "label_removed",
                           f"writes of {edge_type} are refused; its edges stay")
    for index in sorted(set(previous.search_indexes) - set(after.search_indexes)):
        yield Breaking("tenant", None, "search_index_removed",
                       f"searches with index={index!r} are refused")
    for child, parent in sorted(set(previous.label_hierarchy) - set(after.label_hierarchy)):
        yield Breaking("vertex", parent, "hierarchy_removed",
                       f"reads of {parent} no longer include {child} nodes")
    gone_rules = {n for n, _ in previous.resolve_rules} - {n for n, _ in after.resolve_rules}
    for edge_type in sorted(gone_rules):
        yield Breaking("edge", edge_type, "resolve_rule_removed",
                       f"edges:resolve for {edge_type} is refused")


def _endpoint_changes(previous: TenantSpec, after: TenantSpec) -> Iterator[Breaking]:
    if not after.strict_endpoints:
        return
    if not previous.strict_endpoints:
        yield Breaking("tenant", None, "strict_endpoints_on",
                       "writes of a declared edge type outside its declared label pairs "
                       "are refused")
        return
    before = {tuple(p) for p in previous.edge_endpoints}
    for edge_type, src, dst in sorted(before - {tuple(p) for p in after.edge_endpoints}):
        yield Breaking("edge", edge_type, "endpoint_removed",
                       f"writes of {edge_type} from {src} to {dst} are refused")


def _rule_changes(previous: TenantSpec, after: TenantSpec) -> Iterator[Breaking]:
    old, new = properties.policies_for(previous), properties.policies_for(after)
    parts: tuple[tuple[Part, tuple[str, ...], Any, Any], ...] = (
        ("vertex", after.vertex_labels, old.for_vertex, new.for_vertex),
        ("edge", after.edge_labels, old.for_edge, new.for_edge),
    )
    seen: set[tuple[Part, str | None, str]] = set()
    for part, names, old_of, new_of in parts:
        known = set(previous.vertex_labels if part == "vertex" else previous.edge_labels)
        for name in sorted(n for n in names if n in known):
            for found in _policy_changes(part, name, old_of(name), new_of(name)):
                # A rule declared on `*` or a parent label reaches several
                # labels: it is one change, named where it is declared.
                key = (found.part, found.name, found.detail)
                if key not in seen:
                    seen.add(key)
                    yield found


def _refuses(policy: properties.Policy) -> bool:
    """Whether a strict policy can refuse a row at all."""
    return policy.undeclared == "reject" or bool(policy.properties)


def _policy_changes(
    part: Part, name: str, before: properties.Policy, after: properties.Policy,
) -> Iterator[Breaking]:
    if after.mode != "strict" or not _refuses(after):
        return
    if before.mode != "strict":
        yield Breaking(part, name, "mode_strict",
                       f"{name} rows that break a property rule are refused "
                       f"(the mode was {before.mode})")
        return
    if after.undeclared == "reject" and before.undeclared != "reject":
        yield Breaking(part, after.undeclared_source, "undeclared_rejected",
                       f"{after.undeclared_source} rows with undeclared properties are refused")
    for prop, definition in sorted(after.properties.items()):
        was = before.properties.get(prop)
        for change, why in _tighter(was.spec if was else None, definition.spec):
            source = definition.source
            yield Breaking(part, source, change, f"{source}.{prop}: {why}")


def _tighter(
    before: Mapping[str, Any] | None, after: Mapping[str, Any],
) -> Iterator[tuple[str, str]]:
    """``(change, why)`` for each way ``after`` refuses a value ``before`` accepted."""
    if before is None:
        # A newly declared property's other rules hold only keys a row sends;
        # declaring a property is how a schema grows, so only `required` counts.
        if after.get("required"):
            yield "property_added_required", "a new required property"
        return
    if after.get("required") and not before.get("required"):
        yield "property_required", "now required"
    old_type, new_type = before.get("type", "any"), after.get("type", "any")
    if new_type not in (old_type, "any") and (old_type, new_type) not in _WIDENINGS:
        yield "type_changed", f"type {old_type} became {new_type}"
    old_enum, new_enum = before.get("enum"), after.get("enum")
    if new_enum is not None and (old_enum is None or not _values(old_enum) <= _values(new_enum)):
        yield "enum_narrowed", "fewer values allowed"
    for rule in ("pattern", "format", "items"):
        if after.get(rule) is not None and after.get(rule) != before.get(rule):
            yield f"{rule}_changed", f"{rule} rule changed"
    new_min, old_min = after.get("minimum"), before.get("minimum")
    if new_min is not None and (old_min is None or new_min > old_min):
        yield "minimum_raised", f"minimum {new_min:g}"
    new_max, old_max = after.get("maximum"), before.get("maximum")
    if new_max is not None and (old_max is None or new_max < old_max):
        yield "maximum_lowered", f"maximum {new_max:g}"


def _values(values: list[Any]) -> set[str]:
    return {json.dumps(v, sort_keys=True) for v in values}
