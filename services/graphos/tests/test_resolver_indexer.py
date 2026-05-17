"""Tests for ``graphos.resolver.indexer.ensure_resolve_rule_indexes``.

Pure-Python — stubs the asyncpg pool with an in-memory recorder so we
can assert on the DDL graphos chose to issue without needing a live DB.
"""

from __future__ import annotations

import pytest

from graphos.bootstrap import TenantSpec
from graphos.resolver.catalogue import (
    install_catalogue_for_test,
    invalidate_catalogue,
)
from graphos.resolver.indexer import (
    _LAST_RUN,
    IndexerError,
    ensure_resolve_rule_indexes,
)

# ── Pool / Connection stub ──────────────────────────────────────────────


class _StubConn:
    def __init__(self, recorder: list[str]) -> None:
        self._rec = recorder

    async def execute(self, sql: str, *args: object) -> None:
        self._rec.append(sql)


class _StubAcquireCtx:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _StubPool:
    """Just enough of asyncpg.Pool to feed the indexer.

    Records every DDL issued via ``conn.execute``.  Optionally a
    ``patched_catalogue`` keyword on the constructor short-circuits the
    catalogue refresh so we don't need a live DB.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._conn = _StubConn(self.statements)

    def acquire(self) -> _StubAcquireCtx:
        return _StubAcquireCtx(self._conn)


def _patch_catalogue_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``get_catalogue`` return the test-seeded catalogue without
    hitting the DB."""
    from graphos.resolver import catalogue as cat_mod
    from graphos.resolver import indexer as idx_mod

    async def _fake_get(pool, tenant, *, refresh=False):  # noqa: ANN001
        return cat_mod._CACHE[tenant]

    monkeypatch.setattr(idx_mod, "get_catalogue", _fake_get)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def tenant_name() -> str:
    name = "tenant_resolver_idx_test"
    # Defensive setup: the indexer's no-op short-circuit memoises on
    # ``(resolve_rules, meta)``.  Tests within this module reuse the
    # same tenant name and craft different specs that may share a
    # fingerprint; clearing both ends keeps each test independent.
    _LAST_RUN.pop(name, None)
    yield name
    _LAST_RUN.pop(name, None)
    invalidate_catalogue(name)


@pytest.fixture
def pool() -> _StubPool:
    return _StubPool()


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_src_has_array_creates_gin_on_every_carrier_label(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document", "Report"},
        "name": {"Document", "Report"},
        "team": {"Document", "Report"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document", "Report"),
        edge_labels=("REFERENCES",),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "name",
                "require_different": ["team"],
            }),
        ),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )

    gin_doc = "idx_Document_tag_keys_gin"
    gin_report = "idx_Report_tag_keys_gin"
    assert gin_doc in ensured
    assert gin_report in ensured
    # Every GIN DDL targets the right table + JSONB array operator.
    gin_stmts = [s for s in pool.statements if "gin" in s.lower()]
    assert any('"Document"' in s and "-> 'tag_keys'" in s for s in gin_stmts)
    assert any('"Report"' in s and "-> 'tag_keys'" in s for s in gin_stmts)


@pytest.mark.asyncio
async def test_concat_dst_value_creates_btree_per_property(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "collection": {"Tag"},
        "slug": {"Tag"},
        "team": {"Document", "Tag"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document", "Tag"),
        edge_labels=("TAGGED",),
        resolve_rules=(
            ("TAGGED", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "concat(collection, '/', slug)",
                "require_different": ["team"],
            }),
        ),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )

    assert "idx_Tag_collection_btree" in ensured
    assert "idx_Tag_slug_btree" in ensured
    assert "idx_Document_team_btree" in ensured  # require_different (src)
    assert "idx_Tag_team_btree" in ensured       # require_different (dst)
    assert "idx_Document_tag_keys_gin" in ensured  # src array


@pytest.mark.asyncio
async def test_eq_ci_adds_lower_functional_index(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "name": {"Document"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES_CI",),
        resolve_rules=(
            ("REFERENCES_CI", {
                "src_has_array":  "tag_keys",
                "dst_value_from": "name",
                "match_op":       "eq_ci",
            }),
        ),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    assert "idx_Document_name_lower" in ensured
    assert "idx_Document_name_btree" in ensured  # plain still emitted too
    lower_stmts = [s for s in pool.statements if "lower(" in s]
    assert lower_stmts, "expected at least one lower(...) DDL"


@pytest.mark.asyncio
async def test_require_same_and_require_different_get_btree(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "name": {"Document"},
        "team": {"Document"},
        "tenant_id": {"Document"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("CROSS_TENANT_LINK",),
        resolve_rules=(
            ("CROSS_TENANT_LINK", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "name",
                "require_different": ["team"],
                "require_same":      ["tenant_id"],
            }),
        ),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    assert "idx_Document_team_btree" in ensured
    assert "idx_Document_tenant_id_btree" in ensured


@pytest.mark.asyncio
async def test_reregistration_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "name": {"Document"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES",),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":  "tag_keys",
                "dst_value_from": "name",
            }),
        ),
    )

    first = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    n_first = len(pool.statements)
    second = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    n_second = len(pool.statements)
    # Same set of indexes ensured both times.
    assert sorted(first) == sorted(second)
    # Every DDL contains ``IF NOT EXISTS`` so a re-run is a server-side
    # no-op even if the index already exists.
    assert all("IF NOT EXISTS" in s for s in pool.statements)
    # Short-circuit: the second call must hit the in-process cache and
    # issue ZERO additional DDL.  This is the optimisation that closes
    # the multi-second DDL loop a large spec would otherwise repeat.
    assert n_second == n_first, (
        f"second call issued {n_second - n_first} extra DDLs — the "
        f"no-op short-circuit should have returned the cached result"
    )


@pytest.mark.asyncio
async def test_meta_bump_invalidates_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    """Bumping ``spec.meta`` re-derives indexes even when ``resolve_rules``
    are unchanged — the documented escape hatch for "I added a new label
    that carries this property; force a re-run"."""
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "name": {"Document"},
    })
    _patch_catalogue_refresh(monkeypatch)

    base_rules = (
        ("REFERENCES", {
            "src_has_array":  "tag_keys",
            "dst_value_from": "name",
        }),
    )
    spec_a = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES",),
        resolve_rules=base_rules,
        meta={"schema_version": "1"},
    )
    spec_b = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES",),
        resolve_rules=base_rules,
        meta={"schema_version": "2"},  # bumped
    )
    await ensure_resolve_rule_indexes(pool, tenant=tenant_name, spec=spec_a)
    n_after_a = len(pool.statements)
    await ensure_resolve_rule_indexes(pool, tenant=tenant_name, spec=spec_b)
    n_after_b = len(pool.statements)
    # Meta bump must force a re-derivation — DDL count strictly grows.
    assert n_after_b > n_after_a, (
        "meta bump did not invalidate the short-circuit cache"
    )


@pytest.mark.asyncio
async def test_identifier_guard_rejects_unsafe_property(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    install_catalogue_for_test(tenant_name, {})
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("BAD",),
        # ``src_has_array`` validation happens at registration time via
        # the API; here we bypass it to confirm the indexer's own
        # defence-in-depth guard would still catch a malformed rule
        # that somehow reached it.
        resolve_rules=(
            ("BAD", {
                "src_has_array":  "x; DROP TABLE y;",
                "dst_value_from": "name",
            }),
        ),
    )
    with pytest.raises(IndexerError):
        await ensure_resolve_rule_indexes(
            pool, tenant=tenant_name, spec=spec,
        )


@pytest.mark.asyncio
async def test_missing_property_in_catalogue_skips_index(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Catalogue knows nothing about ``tag_keys`` — no label carries it
    # yet.  We must NOT pollute the schema with an index on a missing
    # property; just warn so an operator can re-register later.
    install_catalogue_for_test(tenant_name, {})
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES",),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":  "tag_keys",
                "dst_value_from": "name",
            }),
        ),
    )
    with caplog.at_level("INFO"):
        ensured = await ensure_resolve_rule_indexes(
            pool, tenant=tenant_name, spec=spec,
        )
    assert ensured == []
    assert pool.statements == []
    assert any("not yet present" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_no_resolve_rules_returns_empty(
    tenant_name: str,
    pool: _StubPool,
) -> None:
    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("PLAIN",),
        resolve_rules=(),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    assert ensured == []
    assert pool.statements == []


@pytest.mark.asyncio
async def test_dedupes_across_rules_sharing_property(
    monkeypatch: pytest.MonkeyPatch,
    tenant_name: str,
    pool: _StubPool,
) -> None:
    """Two rules referencing the same scalar property must produce just
    one index, not two."""
    install_catalogue_for_test(tenant_name, {
        "tag_keys": {"Document"},
        "key": {"Document"},
        "name": {"Document"},
        "team": {"Document"},
    })
    _patch_catalogue_refresh(monkeypatch)

    spec = TenantSpec(
        name=tenant_name,
        vertex_labels=("Document",),
        edge_labels=("REFERENCES", "RELATES_TO"),
        resolve_rules=(
            ("REFERENCES", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "name",
                "require_different": ["team"],
            }),
            ("RELATES_TO", {
                "src_has_array":     "tag_keys",
                "dst_value_from":    "key",
                "require_different": ["team"],
            }),
        ),
    )
    ensured = await ensure_resolve_rule_indexes(
        pool, tenant=tenant_name, spec=spec,
    )
    # ``team`` btree appears once; ``tag_keys`` GIN appears once.
    assert ensured.count("idx_Document_team_btree") == 1
    assert ensured.count("idx_Document_tag_keys_gin") == 1
    # The DDL list also has them just once.
    assert sum(1 for s in pool.statements if "idx_Document_team_btree" in s) == 1
    assert sum(1 for s in pool.statements if "idx_Document_tag_keys_gin" in s) == 1
