"""Property names graphos keeps for itself: ``graphos_*``.

graphos writes computed values onto nodes under this prefix (graph metrics:
``graphos_pagerank``, ``graphos_degree``, ``graphos_component``, …), so a
consumer's own data can never collide with them. The rule for consumers is
one line: don't write ``graphos_*``. Every write endpoint refuses a node or
edge property with that prefix, a merge-patch removing one included.

Not checked: graphos's own write-back (``graphos.writes.set_node_metrics``)
and snapshot import, which restores what an export saved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import HTTPException, status

PREFIX = "graphos_"


def reserved_keys(properties: Mapping[str, object] | None) -> list[str]:
    """The ``graphos_*`` keys among ``properties``, sorted."""
    return sorted(k for k in (properties or {}) if k.startswith(PREFIX))


def refusal(keys: Iterable[str]) -> str:
    """The message for a write that uses reserved names."""
    return (
        f"properties {sorted(keys)} are reserved: graphos writes graphos_* properties itself "
        "(graph metrics). Leave them out of writes."
    )


def check(*bags: Mapping[str, object] | None) -> None:
    """Refuse (400) a write whose properties use a ``graphos_*`` name."""
    found = sorted({k for bag in bags for k in reserved_keys(bag)})
    if found:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=refusal(found))
