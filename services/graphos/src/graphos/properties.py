"""Property schema: what each vertex label and edge type carries, and checking
a write against it.

The definitions that apply to a vertex label are, in order, ``*``, each
ancestor from the root down (``label_hierarchy``), then the label's own; an
edge type has ``*`` then its own.  A later entry adds properties or narrows
one, and may not change its type.  They are compiled into one :class:`Policy`
per label or type, cached per spec.

Modes: ``off`` (nothing checked), ``warn`` (checked, written anyway, counted)
and ``strict`` (a row with an error is refused).  ``recommended`` properties
and ``undeclared: warn`` only ever warn.  ``id``, ``graphos_*`` and
``community_id`` are always allowed.
"""

from __future__ import annotations

import builtins
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from graphos import reserved
from graphos.contracts.property_schema import ALL

# A longer value fails a pattern without the pattern being run.
MAX_PATTERN_INPUT = 4096
# Violations listed in a 422 body; the total is always given.
MAX_LISTED = 100

ERROR = "error"
WARNING = "warning"

_SHOWN_CHARS = 60
_ENUM_SHOWN = 8
_CACHE_SIZE = 256


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_IS: dict[str, Callable[[Any], bool]] = {
    "any": lambda v: True,
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": _number,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}

# Checked in order: a bool is also an int in Python.
_NAMES: tuple[tuple[str, str], ...] = (
    ("boolean", "a boolean"), ("integer", "an integer"), ("number", "a number"),
    ("string", "a string"), ("array", "an array"), ("object", "an object"),
)


def _type_name(value: Any) -> str:
    return next((name for kind, name in _NAMES if _IS[kind](value)), type(value).__name__)


def _short(value: Any) -> str:
    text = repr(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _SHOWN_CHARS else text[: _SHOWN_CHARS - 1] + "…"


def _same(value: Any, allowed: Any) -> bool:
    """Equal and of the same kind: ``True`` is not ``1``."""
    return value == allowed and isinstance(value, bool) == isinstance(allowed, bool)


def _formatted(value: str, fmt: str) -> bool:
    """An ISO 8601 date, or a date-time with an offset (``Z`` included)."""
    try:
        if fmt == "date":
            date.fromisoformat(value)
            return True
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _matches(pattern: re.Pattern[str], value: str) -> bool:
    return len(value) <= MAX_PATTERN_INPUT and pattern.fullmatch(value) is not None


def _breaks(pattern: re.Pattern[str] | None, value: Any) -> bool:
    """A string the pattern does not fully match (anything else is not its concern)."""
    return pattern is not None and isinstance(value, str) and not _matches(pattern, value)


def _one_of(value: Any, allowed: list[Any]) -> bool:
    return any(_same(value, a) for a in allowed)


def _listed(values: list[Any]) -> str:
    more = ", …" if len(values) > _ENUM_SHOWN else ""
    return ", ".join(map(str, values[:_ENUM_SHOWN])) + more


@dataclass(frozen=True)
class Violation:
    """One property of one row that breaks its definition."""

    property: str
    rule: str
    message: str
    source: str
    severity: str = ERROR

    @builtins.property
    def key(self) -> str:
        """``<source>.<property>:<rule>``: how violations are counted."""
        return f"{self.source}.{self.property}:{self.rule}"


@dataclass(frozen=True)
class Definition:
    """One property's effective definition, its patterns compiled."""

    spec: Mapping[str, Any]
    source: str
    pattern: re.Pattern[str] | None = None
    item_pattern: re.Pattern[str] | None = None

    @classmethod
    def of(cls, spec: dict[str, Any], source: str) -> Definition:
        items = spec.get("items") or {}
        return cls(
            spec=spec, source=source,
            pattern=re.compile(spec["pattern"]) if spec.get("pattern") else None,
            item_pattern=re.compile(items["pattern"]) if items.get("pattern") else None,
        )

    def problems(self, value: Any) -> list[tuple[str, str]]:
        """``(rule, message)`` for each way a present value breaks this definition."""
        spec = self.spec
        kind = spec.get("type", "any")
        if not _IS[kind](value):
            return [("type", f"{_type_name(value)}, not {kind}")]
        out: list[tuple[str, str]] = []
        enum = spec.get("enum")
        if enum is not None and not _one_of(value, enum):
            out.append(("enum", f"{_short(value)} is not one of {_listed(enum)}"))
        if _breaks(self.pattern, value):
            out.append(("pattern", f"{_short(value)} does not match {spec['pattern']}"))
        fmt = spec.get("format")
        if fmt and isinstance(value, str) and not _formatted(value, fmt):
            out.append(("format", f"{_short(value)} is not an ISO 8601 {fmt}"))
        if _number(value):
            if spec.get("minimum") is not None and value < spec["minimum"]:
                out.append(("minimum", f"{value} is below {spec['minimum']:g}"))
            if spec.get("maximum") is not None and value > spec["maximum"]:
                out.append(("maximum", f"{value} is above {spec['maximum']:g}"))
        if spec.get("items") and isinstance(value, list):
            for i, item in enumerate(value):
                problem = self._item_problem(item)
                if problem:
                    out.append(("items", f"item {i} {problem}"))
                    break
        return out

    def _item_problem(self, item: Any) -> str | None:
        items = self.spec["items"]
        kind = items.get("type", "any")
        if not _IS[kind](item):
            return f"is {_type_name(item)}, not {kind}"
        if items.get("enum") is not None and not _one_of(item, items["enum"]):
            return f"{_short(item)} is not one of the allowed values"
        if _breaks(self.item_pattern, item):
            return f"{_short(item)} does not match {items['pattern']}"
        return None


def always_allowed(name: str) -> bool:
    """Never undeclared: the id, and the properties graphos writes itself."""
    return name == "id" or reserved.is_reserved(name)


@dataclass(frozen=True)
class Policy:
    """The effective definitions of one vertex label or edge type."""

    mode: str = "off"
    undeclared: str = "allow"
    undeclared_source: str = ALL
    properties: Mapping[str, Definition] = field(default_factory=dict)

    @property
    def checks(self) -> bool:
        return self.mode != "off"

    def check(self, properties: Mapping[str, Any], *, row_id: str | None = None) -> list[Violation]:
        """Every violation of a row's properties (as sent, or as a patch leaves
        them), errors and warnings; nothing when the mode is off."""
        if not self.checks:
            return []
        out: list[Violation] = []
        for name, definition in self.properties.items():
            out.extend(self._check_one(name, definition, properties, row_id))
        if self.undeclared != "allow":
            severity = ERROR if self.undeclared == "reject" else WARNING
            out.extend(
                Violation(name, "undeclared", "not declared", self.undeclared_source, severity)
                for name in properties
                if name not in self.properties and not always_allowed(name)
            )
        return out

    @staticmethod
    def _check_one(
        name: str, definition: Definition, properties: Mapping[str, Any], row_id: str | None,
    ) -> list[Violation]:
        spec, source = definition.spec, definition.source
        if name == "id":
            if not _breaks(definition.pattern, row_id):
                return []
            message = f"{_short(row_id)} does not match {spec['pattern']}"
            return [Violation(name, "pattern", message, source)]
        value = properties.get(name)
        if value is None:
            if spec.get("required"):
                return [Violation(name, "required", "required", source)]
            if spec.get("recommended"):
                return [Violation(name, "recommended", "recommended, absent", source, WARNING)]
            return []
        return [
            Violation(name, rule, message, source) for rule, message in definition.problems(value)
        ]


class SchemaError(ValueError):
    """A property schema that cannot be compiled (an inherited type changed)."""


def _ancestors(label: str, hierarchy: Mapping[str, str]) -> list[str]:
    """``label``'s ancestors from the root down."""
    chain: list[str] = []
    seen = {label}
    parent = hierarchy.get(label)
    while parent and parent not in seen:
        chain.append(parent)
        seen.add(parent)
        parent = hierarchy.get(parent)
    return chain[::-1]


def _narrowed(before: Definition, name: str, spec: Mapping[str, Any], source: str) -> Definition:
    """An inherited definition narrowed by a later entry's (keys stated only)."""
    old_type, new_type = before.spec.get("type", "any"), spec.get("type")
    if new_type is not None and old_type not in ("any", new_type):
        raise SchemaError(
            f"{source}.{name} is declared {new_type}, but {before.source} "
            f"declares it {old_type}: a definition may narrow an inherited one, not change its type"
        )
    combined = {**before.spec, **spec}
    combined["required"] = bool(before.spec.get("required") or spec.get("required"))
    if combined["required"]:
        combined.pop("recommended", None)
    return Definition.of(combined, source)


def _merge(entries: Iterable[tuple[str, Mapping[str, Any]]], tenant_mode: str) -> Policy:
    mode, undeclared, undeclared_source = tenant_mode, "allow", ALL
    merged: dict[str, Definition] = {}
    for source, entry in entries:
        mode = entry.get("mode") or mode
        if entry.get("undeclared"):
            undeclared, undeclared_source = entry["undeclared"], source
        for name, spec in (entry.get("properties") or {}).items():
            before = merged.get(name)
            merged[name] = (
                Definition.of(dict(spec), source) if before is None
                else _narrowed(before, name, spec, source)
            )
    return Policy(mode=mode, undeclared=undeclared, undeclared_source=undeclared_source,
                  properties=merged)


@dataclass(frozen=True)
class Policies:
    """The compiled policies of one spec: per vertex label and per edge type."""

    vertex: Mapping[str, Policy]
    edge: Mapping[str, Policy]
    default_vertex: Policy
    default_edge: Policy

    def for_vertex(self, label: str) -> Policy:
        return self.vertex.get(label, self.default_vertex)

    def for_edge(self, edge_type: str) -> Policy:
        return self.edge.get(edge_type, self.default_edge)

    def _mapped(self, change: Callable[[Policy], Policy]) -> Policies:
        return Policies(
            vertex={k: change(p) for k, p in self.vertex.items()},
            edge={k: change(p) for k, p in self.edge.items()},
            default_vertex=change(self.default_vertex), default_edge=change(self.default_edge),
        )

    def relaxed(self) -> Policies:
        """The same checks with none refusing: ``strict`` read as ``warn``."""
        return self._mapped(lambda p: replace(p, mode="warn") if p.mode == "strict" else p)

    def with_mode(self, mode: str) -> Policies:
        """Every label and type checked in ``mode`` (a report reads as if all applied)."""
        return self._mapped(lambda p: replace(p, mode=mode))

    @property
    def any_checks(self) -> bool:
        """Whether any label or type is checked (else writes skip the checks)."""
        every = (*self.vertex.values(), *self.edge.values(), self.default_vertex, self.default_edge)
        return any(p.checks for p in every)


def compile_schema(
    schema: Mapping[str, Any],
    vertex_labels: Iterable[str],
    edge_labels: Iterable[str],
    hierarchy: Mapping[str, str],
) -> Policies:
    """Compile a stored ``property_schema`` for every label and edge type (one
    added later falls back to ``*``).  Raises :class:`SchemaError`."""
    mode = schema.get("mode") or "off"
    vertex_entries: Mapping[str, Any] = schema.get("vertex") or {}
    edge_entries: Mapping[str, Any] = schema.get("edge") or {}

    def chain(entries: Mapping[str, Any], names: Iterable[str]) -> list[tuple[str, Any]]:
        return [(name, entries[name]) for name in (ALL, *names) if name in entries]

    return Policies(
        vertex={
            label: _merge(chain(vertex_entries, [*_ancestors(label, hierarchy), label]), mode)
            for label in vertex_labels
        },
        edge={t: _merge(chain(edge_entries, [t]), mode) for t in edge_labels},
        default_vertex=_merge(chain(vertex_entries, []), mode),
        default_edge=_merge(chain(edge_entries, []), mode),
    )


_cache: dict[str, Policies] = {}


def policies_for(spec: Any) -> Policies:
    """The compiled policies of a ``TenantSpec``, cached per spec content."""
    key = json.dumps(
        [spec.name, spec.property_schema, sorted(spec.vertex_labels), sorted(spec.edge_labels),
         sorted(spec.label_hierarchy)],
        sort_keys=True, default=str,
    )
    found = _cache.get(key)
    if found is None:
        if len(_cache) >= _CACHE_SIZE:
            _cache.clear()
        found = _cache[key] = compile_schema(
            spec.property_schema, spec.vertex_labels, spec.edge_labels, dict(spec.label_hierarchy),
        )
    return found


def effective(policy: Policy) -> dict[str, Any]:
    """A policy as the API serves it (``PropertyDefinitionsResponse``)."""
    return {
        "mode": policy.mode, "undeclared": policy.undeclared,
        "undeclared_source": policy.undeclared_source,
        "properties": {
            name: {"source": d.source, "definition": dict(d.spec)}
            for name, d in policy.properties.items()
        },
    }


def validate_schema(
    schema: Mapping[str, Any],
    vertex_labels: Iterable[str],
    edge_labels: Iterable[str],
    hierarchy: Mapping[str, str],
) -> None:
    """Refuse entries naming a label or type the tenant lacks, and definitions
    that change an inherited type (ValueError)."""
    known = {"vertex": set(vertex_labels), "edge": set(edge_labels)}
    for part, kind in (("vertex", "vertex label"), ("edge", "edge label")):
        for name in schema.get(part) or {}:
            if name != ALL and name not in known[part]:
                raise ValueError(f"property_schema: {name!r} is not a {kind} of the tenant")
    compile_schema(schema, known["vertex"], known["edge"], hierarchy)


def count(violations: Iterable[Violation]) -> tuple[dict[str, int], dict[str, int]]:
    """Errors and warnings counted by ``<source>.<property>:<rule>``."""
    tally = Tally()
    for v in violations:
        tally.note(v, refused=v.severity == ERROR)
    return tally.violations, tally.warnings


class Refused(Exception):  # noqa: N818 — reads as what happened: the write was refused
    """A write refused by the property schema (strict): answered 422."""

    def __init__(self, listed: list[dict[str, Any]], total: int) -> None:
        super().__init__(f"{total} property violations")
        self.listed = listed
        self.total = total


@dataclass
class Tally:
    """What one request's rows broke: ``violations`` refused them (strict),
    ``warnings`` were written anyway (warn, ``recommended``, undeclared)."""

    violations: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)

    def note(self, violation: Violation, *, refused: bool) -> None:
        bucket = self.violations if refused else self.warnings
        bucket[violation.key] = bucket.get(violation.key, 0) + 1

    def add(self, policy: Policy, violations: list[Violation]) -> bool:
        """Count one row's violations; whether the row is refused."""
        refused = policy.mode == "strict" and any(v.severity == ERROR for v in violations)
        for v in violations:
            self.note(v, refused=refused and v.severity == ERROR)
        return refused

    def line(self, policy: Policy, properties: Mapping[str, Any], row_id: str | None) -> str | None:
        """Check one line of a stream: why it is refused, or ``None`` to write it."""
        violations = policy.check(properties, row_id=row_id)
        if not self.add(policy, violations):
            return None
        return "property_schema: " + "; ".join(
            f"{v.source}.{v.property}: {v.message}" for v in violations if v.severity == ERROR
        )

    def report(self) -> dict[str, dict[str, int]]:
        """The response fields: counts by ``<source>.<property>:<rule>``."""
        return {"property_violations": self.violations, "property_warnings": self.warnings}


def described(violation: Violation, **where: Any) -> dict[str, Any]:
    """A violation as listed in a 422 body: where it is, and what is wrong."""
    return {**where, "property": violation.property, "rule": violation.rule,
            "message": violation.message, "source": violation.source}


def node_rows(
    policies: Policies, nodes: Iterable[Any],
) -> Iterable[tuple[Policy, Mapping[str, Any], str | None, dict[str, Any]]]:
    """Upsert rows (``id``, ``label``, ``properties``) as :func:`enforce` takes them."""
    for i, node in enumerate(nodes):
        yield (policies.for_vertex(node.label), node.properties, node.id,
               {"index": i, "id": node.id, "label": node.label})


def edge_rows(
    policies: Policies, edges: Iterable[Any],
) -> Iterable[tuple[Policy, Mapping[str, Any], str | None, dict[str, Any]]]:
    """Edge upsert rows (``type``, ends, ``properties``) as :func:`enforce` takes them."""
    for i, edge in enumerate(edges):
        yield (policies.for_edge(edge.type), edge.properties, None,
               {"index": i, "type": edge.type, "src_id": edge.src_id, "dst_id": edge.dst_id})


RowCheck = Callable[[str, str | None, Mapping[str, Any]], None]


def checker(policies: Policies, part: str) -> RowCheck:
    """A check of one row as a single write leaves it: ``(label or type, id,
    properties)``, raising :class:`Refused` under strict (``graphos.writes``)."""
    def check(name: str, row_id: str | None, props: Mapping[str, Any]) -> None:
        if part == "vertex":
            enforce([(policies.for_vertex(name), props, row_id, {"id": row_id, "label": name})])
        else:
            enforce([(policies.for_edge(name), props, None, {"type": name})])
    return check


def enforce(rows: Iterable[tuple[Policy, Mapping[str, Any], str | None, dict[str, Any]]]) -> Tally:
    """Check rows of one request, each ``(policy, properties, row id, where)``:
    raise :class:`Refused` when any is refused (nothing is to be written), else
    the tally to report."""
    tally = Tally()
    listed: list[dict[str, Any]] = []
    total = 0
    for policy, properties, row_id, where in rows:
        violations = policy.check(properties, row_id=row_id)
        if tally.add(policy, violations):
            errors = [v for v in violations if v.severity == ERROR]
            total += len(errors)
            listed.extend(described(v, **where) for v in errors[: MAX_LISTED - len(listed)])
    if total:
        raise Refused(listed, total)
    return tally


def definitions_count(schema: Mapping[str, Any]) -> int:
    """How many property definitions a schema declares (``*`` included)."""
    return sum(
        len((entry or {}).get("properties") or {})
        for part in ("vertex", "edge") for entry in (schema.get(part) or {}).values()
    )
