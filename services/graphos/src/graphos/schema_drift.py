"""A tenant's schema manager, and the drift report (ADR-0013).

A program that declares a tenant's schema from its code and people who edit
it in the web UI write through the same merge registration. Two things keep
them from undoing each other's work:

- **A schema manager** (``TenantSpec.schema_manager``, stored in
  ``graphos_meta.tenant_settings``): when a tenant names one, only that
  subject's schema writes may change its *structure*; anyone else's write that
  would change more than ``display`` (styles) is refused, 409
  ``schema_managed`` (:class:`SchemaManaged`). Styles stay people's: the
  manager's own registration only adds the ``display`` entries that do not
  exist yet (:func:`keep_peoples_styles`). A write is judged on what it would
  change (:func:`changes_structure`, on ``schema_changes.diff``), so declaring
  again what is stored is never refused.
- **A drift report** (:func:`drift`), in the answer of a registration's dry
  run: what the tenant has that the body does not declare, the entries the
  body would replace with a different value, and the labels, edge types and
  pairs it would create again after they were dropped or removed. Nothing is
  refused for it: what the program declares wins, and the report says so
  (the precedents are in ADR-0013).

Being the manager grants nothing: each write still needs its own level. An
owner sets, transfers or releases the manager through the registration's
``schema_manager`` field.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from graphos import by_entry
from graphos.bootstrap import TenantSpec

# Parts of a schema whose entries people may change on a managed tenant.
PRESENTATION = ("display",)

# The parts declared entry by entry (``by_entry``), as the drift report names them.
_BY_ENTRY = ("display", "property_schema", "deprecated")


class SchemaManaged(Exception):  # noqa: N818 — a refusal, answered 409 (graphos.app)
    """A schema write that would change a managed tenant's structure, by someone
    other than its manager."""

    def __init__(self, tenant: str, manager: str) -> None:
        super().__init__(
            f"the schema of '{tenant}' is managed by {manager}: only its registrations may "
            "change labels, edge types, pairs, properties, indexes or rules (styles may still "
            "be changed); an owner may release it (\"schema_manager\": null)"
        )
        self.tenant = tenant
        self.manager = manager


def check(tenant: str, manager: str | None, subject: str | None) -> None:
    """Refuse when ``manager`` is set and ``subject`` is not it."""
    if manager is not None and subject != manager:
        raise SchemaManaged(tenant, manager)


def changes_structure(detail: dict[str, Any] | None) -> bool:
    """Whether a change (``schema_changes.diff``) touches more than styles."""
    if not detail:
        return False
    for key, value in detail.items():
        if key in ("added", "removed", "changed"):
            if any(part not in PRESENTATION and names for part, names in value.items()):
                return True
        elif key != "registered":
            return True
    return False


def keep_peoples_styles(
    stored: TenantSpec, spec: TenantSpec, removed: by_entry.Removed,
) -> tuple[TenantSpec, by_entry.Removed]:
    """The manager's registration, with only the ``display`` entries the stored
    spec does not have: styles people set are kept, and none is removed."""
    kept = {
        part: {name: entry for name, entry in entries.items()
               if name not in (stored.display.get(part) or {})}
        for part, entries in spec.display.items()
    }
    return replace(spec, display={p: e for p, e in kept.items() if e}), ()


def drift(
    stored: TenantSpec, declared: TenantSpec,
    changes: dict[str, Any] | None, removals: list[dict[str, Any]],
) -> dict[str, Any]:
    """The drift report of a registration declaring ``declared`` on ``stored``.

    ``changes`` is the registration's ``schema_changes.diff`` (its ``changed``
    entries are the overwrites); ``removals`` are the tenant's recorded
    removals. Empty parts are left out.
    """
    report: dict[str, Any] = {}

    not_declared: dict[str, Any] = {}
    for part in ("vertex_labels", "edge_labels"):
        missing = sorted(set(getattr(stored, part)) - set(getattr(declared, part)))
        if missing:
            not_declared[part] = missing
    pairs = sorted(set(stored.edge_endpoints) - set(declared.edge_endpoints))
    if pairs:
        not_declared["edge_endpoints"] = [list(p) for p in pairs]
    indexes = sorted(set(stored.search_indexes) - set(declared.search_indexes))
    if indexes:
        not_declared["search_indexes"] = indexes
    for key in _BY_ENTRY:
        have, declares = getattr(stored, key), getattr(declared, key)
        missing_by_part = {
            part: sorted(set(have.get(part) or {}) - set(declares.get(part) or {}))
            for part in by_entry.PARTS
        }
        missing_by_part = {p: names for p, names in missing_by_part.items() if names}
        if missing_by_part:
            not_declared[key] = missing_by_part
    if not_declared:
        report["not_declared"] = not_declared

    overwrites: dict[str, Any] = {}
    for key, names in ((changes or {}).get("changed") or {}).items():
        if key == "meta":
            overwrites["meta"] = sorted(names)
            continue
        by_part: dict[str, list[str]] = {}
        for qualified in names:
            part, _, name = str(qualified).partition(":")
            by_part.setdefault(part, []).append(name)
        overwrites[key] = {p: sorted(n) for p, n in by_part.items()}
    # A search index declared again replaces the stored definition whole.
    redefined = sorted(
        name for name, body in declared.search_indexes.items()
        if name in stored.search_indexes and stored.search_indexes[name] != body
    )
    if redefined:
        overwrites["search_indexes"] = redefined
    if overwrites:
        report["overwrites"] = overwrites

    recreates = []
    declared_pairs = set(declared.edge_endpoints)
    for r in removals:
        kind, name = r["kind"], r["name"]
        if (
            (kind == "vertex_label" and name in declared.vertex_labels)
            or (kind == "edge_type" and name in declared.edge_labels)
            or (kind == "edge_endpoint"
                and (name, r["src_label"], r["dst_label"]) in declared_pairs)
        ):
            recreates.append({
                "kind": kind, "name": name,
                "src_label": r["src_label"] or None, "dst_label": r["dst_label"] or None,
                "removed_at": r["removed_at"].isoformat() if r["removed_at"] else None,
                "removed_by": r["removed_by"],
            })
    if recreates:
        report["recreates"] = recreates
    return report
