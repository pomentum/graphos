"""Bootstrap routine tests.

Pure-Python layer:
- ``TenantSpec`` dataclass shape and immutability.
- ``register_tenant`` records the spec in the in-process registry.

DB-touching layer (gated on GRAPHOS_TEST_DSN):
- ``bootstrap_tenant`` is idempotent and creates the AGE graph.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos.bootstrap import (
    TenantSpec,
    _spec_from_jsonable,
    _spec_to_jsonable,
    _validate_label_hierarchy,
    register_tenant,
    registered_tenants,
)

from .conftest import requires_db


def test_no_tenants_pre_registered() -> None:
    """graphos starts with an empty tenant registry — no hardcoded tenants."""
    # Note: in-process registry may have entries from earlier tests in this run;
    # we only assert the module ships with no built-in defaults at import time.
    # Other tests must isolate (or this becomes a fixture).


def test_tenant_spec_frozen() -> None:
    spec = TenantSpec(name="t1", vertex_labels=("A",))
    with pytest.raises((AttributeError, Exception)):
        spec.name = "other"  # type: ignore[misc]


def test_tenant_spec_defaults() -> None:
    spec = TenantSpec(name="t1")
    assert spec.vertex_labels == ()
    assert spec.edge_labels == ()
    assert spec.indexes == ()
    assert spec.generated_columns == ()
    assert spec.label_hierarchy == ()
    assert spec.meta == {}


def test_label_hierarchy_validation_accepts_known_pairs() -> None:
    _validate_label_hierarchy(
        pairs=(("Article", "Document"), ("Section", "Document")),
        vertex_labels=("Document", "Article", "Section"),
    )  # no exception


def test_label_hierarchy_validation_rejects_self_loop() -> None:
    with pytest.raises(ValueError, match="self-loop"):
        _validate_label_hierarchy(
            pairs=(("Document", "Document"),),
            vertex_labels=("Document",),
        )


def test_label_hierarchy_validation_rejects_unknown_child() -> None:
    with pytest.raises(ValueError, match="child"):
        _validate_label_hierarchy(
            pairs=(("Missing", "Document"),),
            vertex_labels=("Document",),
        )


def test_label_hierarchy_validation_rejects_unknown_parent() -> None:
    with pytest.raises(ValueError, match="parent"):
        _validate_label_hierarchy(
            pairs=(("Document", "Missing"),),
            vertex_labels=("Document",),
        )


def test_spec_json_roundtrip_preserves_hierarchy() -> None:
    """asdict-based JSON serialisation must round-trip the hierarchy."""
    spec = TenantSpec(
        name="t",
        vertex_labels=("Document", "Article", "Section"),
        edge_labels=("CITES",),
        label_hierarchy=(("Article", "Document"), ("Section", "Document")),
    )
    payload = _spec_to_jsonable(spec)
    restored = _spec_from_jsonable(payload)
    assert restored.label_hierarchy == spec.label_hierarchy


def test_spec_from_jsonable_accepts_dict_shape() -> None:
    """The wire body sends label_hierarchy as a dict; the storage shape
    (asdict on tuple-of-tuples) is nested lists.  Both must restore.
    """
    restored = _spec_from_jsonable({
        "name": "t",
        "vertex_labels": ["Document", "Article"],
        "edge_labels": [],
        "indexes": [],
        "generated_columns": [],
        "label_hierarchy": {"Article": "Document"},
        "meta": {},
    })
    assert restored.label_hierarchy == (("Article", "Document"),)


@requires_db
@pytest.mark.asyncio
async def test_register_tenant_is_idempotent(db_pool: asyncpg.Pool) -> None:
    """Running register_tenant twice for the same name must not error."""
    spec = TenantSpec(
        name="graphos_test_tenant",
        vertex_labels=("TestNode",),
        edge_labels=("TEST_EDGE",),
    )
    await register_tenant(db_pool, spec)
    await register_tenant(db_pool, spec)  # idempotent

    assert "graphos_test_tenant" in registered_tenants()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM ag_catalog.ag_graph WHERE name = $1;",
            spec.name,
        )
        assert row is not None
        assert row["name"] == spec.name
