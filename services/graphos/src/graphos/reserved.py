"""Property names graphos keeps for itself: ``graphos_*`` and ``community_id``.

graphos writes computed values onto nodes: graph metrics under the
``graphos_`` prefix (``graphos_pagerank``, ``graphos_degree``,
``graphos_component``, …) and each node's group from the community recompute
(``community_id``, which edges also copy into their community columns). A
consumer's own data can never collide with them. The rule for consumers is
one line: don't write them. Every write endpoint refuses a node or edge
property with a reserved name, a merge-patch removing one included.

Not checked: graphos's own write-backs (``graphos.writes.set_node_metrics``,
``set_node_communities``) and snapshot and dataset import, which restore what
an export saved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import HTTPException, status

PREFIX = "graphos_"
NAMES = frozenset({"community_id"})


def is_reserved(name: str) -> bool:
    """Whether graphos writes ``name`` itself."""
    return name.startswith(PREFIX) or name in NAMES


def reserved_keys(properties: Mapping[str, object] | None) -> list[str]:
    """The reserved keys among ``properties``, sorted."""
    return sorted(k for k in (properties or {}) if is_reserved(k))


def refusal(keys: Iterable[str]) -> str:
    """The message for a write that uses reserved names."""
    return (
        f"properties {sorted(keys)} are reserved: graphos writes graphos_* (graph metrics) and "
        "community_id (the community recompute) itself. Leave them out of writes."
    )


def check(*bags: Mapping[str, object] | None) -> None:
    """Refuse (400) a write whose properties use a reserved name."""
    found = sorted({k for bag in bags for k in reserved_keys(bag)})
    if found:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=refusal(found))
