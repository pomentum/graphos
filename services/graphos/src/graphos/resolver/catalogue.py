"""Per-tenant property-name → vertex-labels catalogue.

The resolver compiler needs to know, for each property name referenced
in a ``resolve_rule``, which vertex labels carry that property on at
least one row — that's the FROM-list it joins over.

It is read from ``graphos_meta.node_properties`` (:mod:`graphos.node_properties`),
which the write path keeps in the same transaction as the nodes: one
indexed query, current the moment a write commits, with no cache to go
stale.  :func:`catalogue_for_properties` answers for the properties a set
of rules reads (the resolver, the index builder); :func:`refresh_catalogue`
for every property (the ``GET …/graph/catalogue`` diagnostic).

The catalogue is a coarse filter, never authoritative.  A property that a
label's nodes no longer carry may still be listed for it (the record only
grows), and the resolver then finds no matching rows.  An empty entry for
a property name means "no node carries this property *yet*": the rule
compiles to nothing and writes zero edges, the design-doc-specified
behaviour for a rule that references a not-yet-present property.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import asyncpg

from graphos import node_properties

log = logging.getLogger(__name__)

# Same identifier shape we accept in resolve_rule property names.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class PropertyLabelCatalogue:
    """One tenant's property→labels map.

    ``by_property[prop_name]`` is the set of vertex labels (child
    tables under ``_ag_label_vertex``) that have at least one row
    carrying ``prop_name`` as a top-level JSONB key.
    """

    tenant: str
    by_property: dict[str, frozenset[str]] = field(default_factory=dict)

    def labels_for(self, prop_name: str) -> frozenset[str]:
        return self.by_property.get(prop_name, frozenset())


async def _discover_vertex_labels(
    conn: asyncpg.Connection,
    tenant: str,
) -> list[str]:
    """Return the child vertex labels under ``_ag_label_vertex`` for
    one tenant — i.e. every concrete vertex label table the consumer
    has registered.  Ignores the AGE-internal parent table.
    """
    rows = await conn.fetch(
        "SELECT c.relname AS label "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE n.nspname = $1 AND c.relkind = 'r' "
        "AND p.relname = '_ag_label_vertex' "
        "ORDER BY c.relname;",
        tenant,
    )
    out: list[str] = []
    for r in rows:
        label = str(r["label"])
        # Defence-in-depth: only ever return identifier-safe labels
        # so callers can interpolate them into quoted table names.
        if _IDENT.fullmatch(label):
            out.append(label)
    return out


async def _discover_label_parents(
    conn: asyncpg.Connection,
    tenant: str,
) -> dict[str, frozenset[str]]:
    """Map ``child_label`` → set of ancestor vertex labels in the
    tenant schema (excluding ``_ag_label_vertex`` itself).

    When a consumer registers ``label_hierarchy``, graphos installs
    PostgreSQL table inheritance so writes under the child label are
    visible to scans of the parent.  The catalogue MUST account for
    that: a property declared on a child table also appears in any
    ``SELECT FROM parent`` scan because postgres's inheritance walks
    every descendant.

    Resolving "labels that carry property P" by naive scan therefore
    over-reports inheritance parents — and the compiler emitting
    ``FROM parent_table src CROSS JOIN parent_table dst`` then asks
    postgres to scan the whole descendant union on both sides of the
    join (~300 k × 300 k cardinality on a large graph), which
    OOMs the dynamic shared-memory segment.

    Returning the ancestor map here lets ``refresh_catalogue`` prune
    parents from ``by_property[prop]`` when any of their descendants
    already carries ``prop``, so the resolver only ever joins over
    leaf labels.  Child × child INSERTs cover the same data without
    the multiplicative cross-product explosion.
    """
    rows = await conn.fetch(
        "SELECT c.relname AS child, p.relname AS parent "
        "FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace nc ON nc.oid = c.relnamespace "
        "JOIN pg_namespace np ON np.oid = p.relnamespace "
        "WHERE nc.nspname = $1 AND np.nspname = $1 "
        "AND c.relkind = 'r' AND p.relkind = 'r';",
        tenant,
    )
    direct: dict[str, set[str]] = {}
    for r in rows:
        child = str(r["child"])
        parent = str(r["parent"])
        if not (_IDENT.fullmatch(child) and _IDENT.fullmatch(parent)):
            continue
        direct.setdefault(child, set()).add(parent)
    # Transitively walk the chain so a grand-parent is pruned too.
    out: dict[str, frozenset[str]] = {}
    for child in direct:
        seen: set[str] = set()
        stack = list(direct.get(child, set()))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            stack.extend(direct.get(p, set()))
        out[child] = frozenset(seen)
    return out


def _prune_parents(
    raw: dict[str, set[str]], ancestors: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Leaf labels only: drop a parent label when one of its descendants
    carries the same property.

    Postgres expands ``SELECT FROM parent`` to scan every descendant, so
    a compiler joining over the parent would scan the rows the child ×
    child INSERTs already cover, multiplied by the descendant count on
    both sides.  Leaf-only candidates give the planner a tight
    cardinality estimate and let the composite indexes do their job.
    """
    out: dict[str, frozenset[str]] = {}
    for prop, label_set in raw.items():
        pruned = set(label_set)
        for cand in label_set:
            descendants = {child for child, anc in ancestors.items() if cand in anc}
            if descendants & label_set:
                pruned.discard(cand)
        out[prop] = frozenset(pruned) if pruned else frozenset(label_set)
    return out


async def _catalogue(
    pool: asyncpg.Pool, tenant: str, names: set[str] | None,
) -> PropertyLabelCatalogue:
    """The catalogue for ``names`` (every recorded name when None).

    Labels that no longer exist are left out, and inheritance parents are
    pruned.  Best-effort: a database error yields an empty catalogue.
    """
    cat = PropertyLabelCatalogue(tenant=tenant)
    try:
        await node_properties.ensure_complete(pool, tenant)
        async with pool.acquire() as conn:
            labels = set(await _discover_vertex_labels(conn, tenant))
            ancestors = await _discover_label_parents(conn, tenant)
            recorded = await node_properties.labels_by_property(conn, tenant, names)
        raw = {
            prop: found & labels
            for prop, found in recorded.items()
            # The compiler only emits identifier-safe property paths.
            if _IDENT.fullmatch(prop) and found & labels
        }
        cat.by_property = _prune_parents(raw, ancestors)
    except asyncpg.exceptions.PostgresError as exc:
        log.warning(
            "catalogue lookup failed (using empty)", extra={"tenant": tenant, "error": str(exc)},
        )
    return cat


async def catalogue_for_properties(
    pool: asyncpg.Pool, tenant: str, properties: Iterable[str],
) -> PropertyLabelCatalogue:
    """The labels carrying each of ``properties``, as of the last commit.

    Names that are not plain identifiers are ignored, as the compiler
    would reject them.
    """
    wanted = {p for p in properties if _IDENT.fullmatch(p)}
    if not wanted:
        return PropertyLabelCatalogue(tenant=tenant)
    return await _catalogue(pool, tenant, wanted)


async def refresh_catalogue(
    pool: asyncpg.Pool, tenant: str, *, rebuild: bool = False,
) -> PropertyLabelCatalogue:
    """Every recorded property of the tenant, with its labels.

    ``rebuild`` first re-reads every node (:func:`graphos.node_properties.backfill`),
    dropping names no node carries any more; otherwise the record is read as is.
    """
    if rebuild:
        await node_properties.backfill(pool, tenant, rebuild=True)
    return await _catalogue(pool, tenant, None)


async def get_catalogue(
    pool: asyncpg.Pool, tenant: str, *, refresh: bool = False,
) -> PropertyLabelCatalogue:
    """Every property of the tenant; ``refresh`` rebuilds the record first."""
    return await refresh_catalogue(pool, tenant, rebuild=refresh)


# Catalogues tests install in place of a database lookup.
_TEST_CATALOGUES: dict[str, PropertyLabelCatalogue] = {}


def install_catalogue_for_test(tenant: str, by_property: dict[str, set[str]]) -> None:
    """Test-only: a fixed catalogue, returned by stubbed lookups."""
    _TEST_CATALOGUES[tenant] = PropertyLabelCatalogue(
        tenant=tenant,
        by_property={k: frozenset(v) for k, v in by_property.items()},
    )


def forget_catalogue_for_test(tenant: str) -> None:
    _TEST_CATALOGUES.pop(tenant, None)
