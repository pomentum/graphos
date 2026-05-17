"""Schema-driven property-match edge resolver.

Compiles a tenant-registered ``resolve_rule`` JSONB body into a single
parameterised SQL statement that derives edges by joining two vertex
tables on a property-value match.  See ``compiler.py`` for the DSL
shape and the compilation contract, and ``catalogue.py`` for the
property-name → vertex-labels map that drives FROM-list inference.

graphos stays domain-agnostic: this package compiles, executes, and
counts.  It never inspects what a property name *means*.
"""

from __future__ import annotations

from graphos.resolver.catalogue import (
    PropertyLabelCatalogue,
    get_catalogue,
    refresh_catalogue,
)
from graphos.resolver.compiler import (
    CompiledEdgeType,
    CompiledRule,
    CompileError,
    compile_resolve_rule,
    compile_resolve_rules_for_edge_type,
)
from graphos.resolver.indexer import (
    IndexerError,
    ensure_resolve_rule_indexes,
)

__all__ = [
    "CompiledEdgeType",
    "CompiledRule",
    "CompileError",
    "IndexerError",
    "PropertyLabelCatalogue",
    "compile_resolve_rule",
    "compile_resolve_rules_for_edge_type",
    "ensure_resolve_rule_indexes",
    "get_catalogue",
    "refresh_catalogue",
]
