"""Per-tenant property-name → vertex-labels catalogue.

The resolver compiler needs to know, for each property name referenced
in a ``resolve_rule``, which vertex labels carry that property on at
least one row — that's the FROM-list it joins over.  The catalogue is a
small in-process map kept per tenant, refreshed on tenant registration
and on demand from the resolve endpoint when a cached entry is missing
or stale.

The catalogue is a soft cache, never authoritative.  An empty entry for
a property name means "no node carries this property *yet*" and the
resolver will still attempt the compile — at worst, the rule fires on
the parent ``_ag_label_vertex`` table and writes zero edges, which is
the design-doc-specified behaviour for a rule that references a not-yet-
present property.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import asyncpg

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


# In-process cache.  Refresh is cheap (one SQL per tenant) so we don't
# bother with a TTL — invalidate on registration + on-demand by the
# resolve endpoint.
_CACHE: dict[str, PropertyLabelCatalogue] = {}
_CACHE_LOCK = asyncio.Lock()


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


async def _properties_present_on(
    conn: asyncpg.Connection,
    tenant: str,
    label: str,
) -> frozenset[str]:
    """Return the set of top-level JSONB keys that appear on at least
    one row in ``"<tenant>"."<label>"``.  Cheap aggregate — uses the
    same per-label GIN(properties) index installed at bootstrap; on
    empty tables it short-circuits.
    """
    # AGE stores ``properties`` as ``agtype``; cast to ``jsonb`` for
    # ``jsonb_object_keys``.  ``LIMIT`` after the DISTINCT keeps the
    # cost bounded even on a giant table — we just need any node with
    # the key.
    row = await conn.fetchrow(
        f'''
        SELECT COALESCE(
          jsonb_agg(DISTINCT k),
          '[]'::jsonb
        ) AS keys
        FROM "{tenant}"."{label}",
             LATERAL jsonb_object_keys(
               (properties::text)::jsonb
             ) AS k;
        '''
    )
    if row is None or row["keys"] is None:
        return frozenset()
    keys = json.loads(row["keys"]) if isinstance(row["keys"], str) else list(row["keys"])
    return frozenset(str(k) for k in keys)


async def refresh_catalogue(
    pool: asyncpg.Pool,
    tenant: str,
) -> PropertyLabelCatalogue:
    """Rebuild the catalogue for one tenant from live DB state.

    Best-effort: errors from a single label are logged and skipped so a
    partially-bootstrapped tenant doesn't poison the catalogue for the
    rest.  Returns the freshly-built entry.
    """
    cat = PropertyLabelCatalogue(tenant=tenant)
    try:
        async with pool.acquire() as conn:
            labels = await _discover_vertex_labels(conn, tenant)
            ancestors = await _discover_label_parents(conn, tenant)
            raw: dict[str, set[str]] = {}
            for label in labels:
                try:
                    keys = await _properties_present_on(conn, tenant, label)
                except asyncpg.exceptions.PostgresError as exc:
                    log.debug(
                        "catalogue: skipping label",
                        extra={
                            "tenant": tenant, "label": label, "error": str(exc),
                        },
                    )
                    continue
                for prop in keys:
                    if not _IDENT.fullmatch(prop):
                        # The catalogue is consumed by the compiler,
                        # which only emits identifier-safe paths.
                        # Skip oddly-named keys so they can't sneak
                        # through.
                        continue
                    raw.setdefault(prop, set()).add(label)
            # Prune inheritance parents.  When a property appears on
            # both a parent label and any of its descendants, drop the
            # parent from the candidate set: postgres expands
            # ``SELECT FROM parent`` to scan every descendant anyway,
            # so the compiler joining over the parent would scan the
            # same rows the child × child INSERTs already cover —
            # multiplied by the descendant count on both sides.  Leaf-
            # only candidates give the planner a tight cardinality
            # estimate and let the composite indexes do their job.
            for prop, label_set in raw.items():
                pruned: set[str] = set(label_set)
                for cand in list(label_set):
                    cand_descendants = {
                        child
                        for child, anc in ancestors.items()
                        if cand in anc
                    }
                    if cand_descendants & label_set:
                        pruned.discard(cand)
                cat.by_property[prop] = (
                    frozenset(pruned) if pruned else frozenset(label_set)
                )
    except asyncpg.exceptions.PostgresError as exc:
        log.warning(
            "catalogue refresh failed (using empty)",
            extra={"tenant": tenant, "error": str(exc)},
        )
    async with _CACHE_LOCK:
        _CACHE[tenant] = cat
    return cat


async def get_catalogue(
    pool: asyncpg.Pool,
    tenant: str,
    *,
    refresh: bool = False,
) -> PropertyLabelCatalogue:
    """Return the cached catalogue for one tenant, refreshing on miss
    or when ``refresh=True``.
    """
    if not refresh:
        async with _CACHE_LOCK:
            cached = _CACHE.get(tenant)
        if cached is not None:
            return cached
    return await refresh_catalogue(pool, tenant)


def invalidate_catalogue(tenant: str) -> None:
    """Drop one tenant's cached catalogue.  Used by tests; also safe
    to call from any code path that mutates the registered schema.
    """
    _CACHE.pop(tenant, None)


def install_catalogue_for_test(
    tenant: str,
    by_property: dict[str, set[str]],
) -> None:
    """Test-only seed: install a fixed catalogue without hitting the DB."""
    _CACHE[tenant] = PropertyLabelCatalogue(
        tenant=tenant,
        by_property={k: frozenset(v) for k, v in by_property.items()},
    )
