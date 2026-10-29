"""A tenant's schema manager, and the drift report (ADR-0013, amended by ADR-0014).

A program that declares a tenant's schema from its code and people who edit
it in the web UI write through the same merge registration. Two things keep
them from undoing each other's work:

- **A schema manager** (``TenantSpec.schema_manager``, stored in
  ``graphos_meta.tenant_settings``): when a tenant names one, only that
  subject's schema writes may change its *structure*; anyone else's write that
  would change more than ``display`` (styles) is refused, 409
  ``schema_managed`` (:class:`SchemaManaged`). A write is judged on what it
  would change (:func:`changes_structure`, on ``schema_changes.diff``), so
  declaring again what is stored is never refused. The community algorithm
  is an operational setting, not structure.
- **Styles merge as the registration says** (:func:`apply_display_merge`):
  ``entries``, the merge every registration has, or ``missing_only``, which
  adds only the styles the tenant does not have and keeps every stored one,
  in a replace too. The manager's registrations default to ``missing_only``,
  so people's styles survive its starts unless it chooses otherwise.
- **A drift report** (:func:`drift`) in the answer of a registration's dry
  run: what the tenant has that the body does not declare, what a replace
  would remove, the entries it would overwrite, the settings it would change,
  and the labels, edge types and pairs it would create again after they were
  dropped or removed. Nothing is refused for it: what the program declares
  wins, and the report says what that changes. It names items as the change
  feed does (``schema_changes``).

Managers are ``user:<name>`` (a graphos user or service account) or
``oidc-sub:<sub>`` (a caller signed in through the identity provider): see
:func:`principal_subject`. Being the manager grants nothing: each write still
needs its own level.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from graphos import by_entry, schema_changes
from graphos.bootstrap import TenantSpec
from graphos.identity import Principal

# Parts of a schema whose entries people may change on a managed tenant.
PRESENTATION = ("display",)

DisplayMerge = Literal["entries", "missing_only"]

# The parts that can be dropped or removed, as the removal records name them.
REMOVAL_PARTS = {
    "vertex_label": "vertex_labels", "edge_type": "edge_labels", "edge_endpoint": "edge_endpoints",
}


class SchemaManaged(Exception):  # noqa: N818 — a refusal, answered 409 (graphos.app)
    """A schema write that would change a managed tenant's structure, by someone
    other than its manager."""

    def __init__(self, tenant: str, manager: str) -> None:
        super().__init__(
            f"the schema of '{tenant}' is managed by {manager}: only its registrations may "
            "change labels, edge types, pairs, properties, indexes, rules or settings (styles "
            "may still be changed); an owner may release it (\"schema_manager\": null)"
        )
        self.tenant = tenant
        self.manager = manager


def principal_subject(principal: Principal) -> str | None:
    """How a caller is named as a schema manager: ``user:<name>`` for graphos
    users and service accounts, ``oidc-sub:<sub>`` for callers signed in through
    the identity provider, ``None`` for the anonymous admin.  Both are the
    principal's own grant subject (``identity``, ``oidc.Identity.subjects``)."""
    subject = principal.subject
    if subject is None or not subject.startswith(("user:", "oidc-sub:")):
        return None
    return subject


def check(tenant: str, manager: str | None, principal: Principal) -> None:
    """Refuse when ``manager`` is set and the caller is not it."""
    if manager is not None and principal_subject(principal) != manager:
        raise SchemaManaged(tenant, manager)


def changes_structure(detail: dict[str, Any] | None) -> bool:
    """Whether a change (``schema_changes.diff``) touches more than styles."""
    if not detail:
        return False
    for key, value in detail.items():
        if key in ("added", "removed", "changed"):
            if any(part not in PRESENTATION and names for part, names in value.items()):
                return True
        elif key not in ("registered", "schema_manager"):
            return True
    return False


def apply_display_merge(
    stored: TenantSpec | None, spec: TenantSpec, removed: by_entry.Removed,
    mode: DisplayMerge, *, replace_all: bool,
) -> tuple[TenantSpec, by_entry.Removed]:
    """A registration's styles as its merge mode says: ``entries`` as sent;
    ``missing_only`` with only the entries the stored spec lacks and none
    removed, the stored ones kept when the registration replaces the spec, for
    the labels and edge types it still has (a replace drops the others)."""
    if mode == "entries" or stored is None:
        return spec, removed
    new = {
        part: {name: entry for name, entry in entries.items()
               if name not in (stored.display.get(part) or {})}
        for part, entries in spec.display.items()
    }
    if replace_all:
        kept = {"vertex": set(spec.vertex_labels), "edge": set(spec.edge_labels)}
        new = {
            part: {
                **{name: entry for name, entry in (stored.display.get(part) or {}).items()
                   if name in kept.get(part, ())},
                **(new.get(part) or {}),
            }
            for part in {*stored.display, *new}
        }
    return replace(spec, display={p: e for p, e in new.items() if e}), ()


def _named(field: str, items: list[Any]) -> Any:
    """Items of one part as the report shows them: by-entry parts per vertex
    and edge (``{"vertex": ["Doc"]}``), the others as the change feed does."""
    if field not in schema_changes.BY_ENTRY:
        return items
    out: dict[str, list[str]] = {}
    for qualified in items:
        part, _, name = str(qualified).partition(":")
        out.setdefault(part, []).append(name)
    return {p: sorted(n) for p, n in out.items()}


def drift(
    stored: TenantSpec, declared: TenantSpec, after: TenantSpec, *, replace_all: bool,
    removals: list[dict[str, Any]],
) -> dict[str, Any]:
    """The drift report of a registration declaring ``declared`` on ``stored``,
    leaving ``after``; ``removals`` are the tenant's recorded removals. Empty
    parts are left out."""
    changes = schema_changes.diff(stored, after) or {}
    report: dict[str, Any] = {}

    if replace_all:
        removes = {f: _named(f, v) for f, v in (changes.get("removed") or {}).items() if v}
        if removes:
            report["removes"] = removes
    else:
        have, declares = schema_changes.parts(stored), schema_changes.parts(declared)
        not_declared = {
            field: _named(field, schema_changes.shown(field, items - declares.get(field, set())))
            for field, items in have.items() if items - declares.get(field, set())
        }
        if not_declared:
            report["not_declared"] = not_declared

    overwrites = {f: _named(f, v) for f, v in (changes.get("changed") or {}).items() if v}
    if overwrites:
        report["overwrites"] = overwrites

    settings = {k: changes[k] for k in ("strict_endpoints", "property_mode") if k in changes}
    if declared.community_algorithm and declared.community_algorithm != stored.community_algorithm:
        settings["community_algorithm"] = [stored.community_algorithm, declared.community_algorithm]
    if settings:
        report["settings"] = settings

    recreates: dict[str, list[dict[str, Any]]] = {}
    declared_parts = schema_changes.parts(declared)
    # A name the tenant has again (a rename brought it back) is not recreated.
    stored_parts = schema_changes.parts(stored)
    for r in removals:
        part = REMOVAL_PARTS.get(r["kind"])
        if part is None:
            continue
        pair = part == "edge_endpoints"
        item = (r["name"], r["src_label"], r["dst_label"]) if pair else r["name"]
        if item not in declared_parts.get(part, set()) or item in stored_parts.get(part, set()):
            continue
        entry: dict[str, Any] = (
            {"pair": list(item)} if part == "edge_endpoints" else {"name": item}
        )
        entry["removed_at"] = r["removed_at"].isoformat() if r["removed_at"] else None
        entry["removed_by"] = r["removed_by"]
        recreates.setdefault(part, []).append(entry)
    if recreates:
        report["recreates"] = recreates
    return report
