"""Declared relationship endpoints: which label pairs an edge type may join.

A tenant declares, per edge type, the ``(source label, target label)``
pairs it may connect (``edge_endpoints`` on ``POST /v1/tenants``).  The
declarations are the schema's associations: ``GET /v1/{tenant}/schema``
serves them and the UI draws them.  With ``strict_endpoints`` on, a write
of a declared type outside its pairs is refused; a type with no declared
pairs stays open, so declaring is opt-in per type.

A child label (``label_hierarchy``) satisfies a pair declared for any of
its ancestors: declaring ``File -[DEFINES]-> Symbol`` allows ``File ->
Function`` when ``Function`` inherits ``Symbol``.

The refusal message says "not declared" on purpose: clients re-register a
tenant when an error says a label is "not registered" or "does not exist"
(schema drift), which would not help here and must not be triggered.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


def _ancestry(label: str, parents: dict[str, str]) -> tuple[str, ...]:
    """``label`` and its ancestors, nearest first (cycle-safe)."""
    chain = [label]
    seen = {label}
    current = parents.get(label)
    while current is not None and current not in seen:
        chain.append(current)
        seen.add(current)
        current = parents.get(current)
    return tuple(chain)


@dataclass(slots=True)
class EndpointPolicy:
    """The declared pairs of one tenant, ready to check writes against."""

    declared: dict[str, frozenset[tuple[str, str]]]
    parents: dict[str, str]
    strict: bool
    _ancestry: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _verdicts: dict[tuple[str, str, str], bool] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        endpoints: Iterable[tuple[str, str, str]],
        hierarchy: Iterable[tuple[str, str]],
        *,
        strict: bool,
    ) -> EndpointPolicy:
        declared: dict[str, set[tuple[str, str]]] = {}
        for edge_type, src, dst in endpoints:
            declared.setdefault(edge_type, set()).add((src, dst))
        return cls(
            declared={t: frozenset(p) for t, p in declared.items()},
            parents=dict(hierarchy),
            strict=strict,
        )

    def _lineage(self, label: str) -> tuple[str, ...]:
        found = self._ancestry.get(label)
        if found is None:
            found = self._ancestry[label] = _ancestry(label, self.parents)
        return found

    def allows(self, edge_type: str, src_label: str, dst_label: str) -> bool:
        """Whether the pair is declared for the type, or the type declares none."""
        key = (edge_type, src_label, dst_label)
        verdict = self._verdicts.get(key)
        if verdict is None:
            pairs = self.declared.get(edge_type)
            verdict = pairs is None or any(
                (s, d) in pairs for s in self._lineage(src_label) for d in self._lineage(dst_label)
            )
            self._verdicts[key] = verdict
        return verdict

    def refusal(self, edge_type: str, src_label: str, dst_label: str) -> str | None:
        """Why a write is refused, or None when it may go ahead."""
        if not self.strict or self.allows(edge_type, src_label, dst_label):
            return None
        pairs = ", ".join(f"{s}->{d}" for s, d in sorted(self.declared[edge_type]))
        return (
            f"{src_label}-[{edge_type}]->{dst_label} is not declared: {edge_type} may join "
            f"{pairs}. Declare the pair (edge_endpoints on POST /v1/tenants) or turn "
            "strict_endpoints off."
        )


def policy_for(spec: object) -> EndpointPolicy:
    """The endpoint policy of a registered tenant spec."""
    return EndpointPolicy.build(
        getattr(spec, "edge_endpoints", ()),
        getattr(spec, "label_hierarchy", ()),
        strict=bool(getattr(spec, "strict_endpoints", False)),
    )
