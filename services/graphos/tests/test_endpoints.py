"""Declared relationship endpoints: the policy, the contract, the resolver filter.

No database: these are the rules ``graphos.endpoints`` applies to a write,
the request model's checks, and the resolver leaving out undeclared pairs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from graphos.contracts.tenants import MAX_EDGE_ENDPOINTS, EdgeEndpointSpec, TenantSpecBody
from graphos.endpoints import EndpointPolicy
from graphos.resolver.catalogue import PropertyLabelCatalogue
from graphos.resolver.compiler import compile_resolve_rules_for_edge_type

HIERARCHY = [("Function", "Symbol"), ("Method", "Function"), ("Class", "Symbol")]


def _policy(*endpoints: tuple[str, str, str], strict: bool = True) -> EndpointPolicy:
    return EndpointPolicy.build(endpoints, HIERARCHY, strict=strict)


def test_a_type_with_no_declared_pairs_stays_open() -> None:
    policy = _policy(("DEFINES", "File", "Symbol"))
    assert policy.allows("LINKS", "Repo", "File")
    assert policy.refusal("LINKS", "Repo", "File") is None


def test_declared_pairs_and_the_labels_inheriting_them() -> None:
    policy = _policy(("DEFINES", "File", "Symbol"), ("CALLS", "Function", "Function"))
    assert policy.allows("DEFINES", "File", "Symbol")
    assert policy.allows("DEFINES", "File", "Class")        # Class inherits Symbol
    assert policy.allows("DEFINES", "File", "Method")       # Method -> Function -> Symbol
    assert policy.allows("CALLS", "Method", "Function")     # on the source side too
    assert not policy.allows("DEFINES", "Repo", "Class")
    assert not policy.allows("CALLS", "Class", "Function")  # Class is no Function
    assert not policy.allows("CALLS", "Symbol", "Function")  # a parent is not its child


def test_strict_off_never_refuses() -> None:
    policy = _policy(("DEFINES", "File", "Symbol"), strict=False)
    assert not policy.allows("DEFINES", "Repo", "File")
    assert policy.refusal("DEFINES", "Repo", "File") is None


def test_a_cycle_in_the_hierarchy_does_not_hang() -> None:
    policy = EndpointPolicy.build([("R", "A", "A")], [("A", "B"), ("B", "A")], strict=True)
    assert policy.allows("R", "B", "B")
    assert not policy.allows("R", "C", "A")


def test_the_refusal_names_the_declared_pairs_without_looking_like_drift() -> None:
    why = _policy(("DEFINES", "File", "Symbol"), ("DEFINES", "Module", "Symbol")).refusal(
        "DEFINES", "Repo", "Class",
    )
    assert why is not None
    assert "Repo-[DEFINES]->Class is not declared" in why
    assert "File->Symbol, Module->Symbol" in why
    # Clients re-register a tenant on these words (schema drift, see
    # graphos_client.is_schema_drift); a refusal must not set that off.
    for phrase in ("not registered", "does not exist", "relation"):
        assert phrase not in why.lower()


def test_an_endpoint_pair_reads_as_an_object_or_a_pair() -> None:
    assert EdgeEndpointSpec.model_validate(["File", "Symbol"]) == EdgeEndpointSpec(
        src="File", dst="Symbol",
    )
    with pytest.raises(ValidationError):
        EdgeEndpointSpec.model_validate(["File"])
    with pytest.raises(ValidationError):
        EdgeEndpointSpec.model_validate({"src": "file", "dst": "Symbol"})


def test_the_registration_body_checks_and_dedupes_declared_pairs() -> None:
    body = TenantSpecBody(name="t", edge_endpoints={
        "DEFINES": [["File", "Symbol"], {"src": "File", "dst": "Symbol"}, ["Module", "Symbol"]],
    })
    assert [(p.src, p.dst) for p in body.edge_endpoints["DEFINES"]] == [
        ("File", "Symbol"), ("Module", "Symbol"),
    ]
    assert body.strict_endpoints is None                 # omitted: leave as stored
    assert "edge_endpoints" in body.model_fields_set
    assert "edge_endpoints" not in TenantSpecBody(name="t").model_fields_set
    with pytest.raises(ValidationError, match="edge_endpoints type"):
        TenantSpecBody(name="t", edge_endpoints={"defines": [["File", "Symbol"]]})
    with pytest.raises(ValidationError, match="at most"):
        TenantSpecBody(name="t", edge_endpoints={
            "DEFINES": [[f"L{i}", "Symbol"] for i in range(MAX_EDGE_ENDPOINTS + 1)],
        })


def test_the_resolver_leaves_out_pairs_the_policy_refuses() -> None:
    catalogue = PropertyLabelCatalogue(tenant="t", by_property={
        "call_sites": frozenset({"Function"}),
        "name": frozenset({"Function", "File"}),
    })
    rule = {"src_has_array": "call_sites", "dst_value_from": "name"}
    every = compile_resolve_rules_for_edge_type(
        tenant="t", edge_type="CALLS", rules=[rule], catalogue=catalogue,
    )
    assert every.arm_count == 2 and every.undeclared_arm_count == 0
    policy = _policy(("CALLS", "Function", "Function"))
    declared = compile_resolve_rules_for_edge_type(
        tenant="t", edge_type="CALLS", rules=[rule], catalogue=catalogue,
        allows=lambda s, d: policy.allows("CALLS", s, d),
    )
    assert declared.arm_count == 1 and declared.undeclared_arm_count == 1
    assert '"t"."File"' not in (declared.sql or "")
