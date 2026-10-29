"""A tenant's schema manager and the drift report, over a mock transport.

``schema_manager`` is read from the tenant and the schema and sent with a
registration or an import; ``ensure_tenant`` runs a registration's dry run,
logs its ``drift`` and registers (or fails, or skips the dry run).
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from graphos_client import GraphosClient, GraphosError, PasswordAuth, SchemaDriftError

_DRIFT_LOG = "graphos_client.drift"

# Every part of a drift report, one item each.
_FULL_REPORT = {
    "not_declared": {
        "vertex_labels": ["Team"],
        "edge_labels": ["MENTIONS"],
        "edge_endpoints": [["OWNS", "Team", "Doc"]],
        "search_indexes": ["titles"],
        "display": {"vertex": ["Team"], "edge": []},
        "property_schema": {"vertex": [], "edge": ["OWNS"]},
        "deprecated": {"vertex": ["Draft"], "edge": []},
    },
    "overwrites": {
        "display": {"vertex": ["Doc"], "edge": []},
        "property_schema": {"vertex": ["Doc"]},
        "meta": ["owner"],
        "search_indexes": ["names"],
    },
    "recreates": [
        {"kind": "edge_type", "name": "OLD", "src_label": None, "dst_label": None,
         "removed_at": "2026-10-20T09:30:00Z", "removed_by": "olive"},
        {"kind": "edge_endpoint", "name": "LINKS_TO", "src_label": "Doc", "dst_label": "Doc",
         "removed_at": "2026-10-21T10:00:00Z", "removed_by": "olive"},
        {"kind": "vertex_label", "name": "Old"},
    ],
}

_FULL_LINES = [
    "acme: vertex label 'Team' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: edge type 'MENTIONS' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: label pair Team-[OWNS]->Doc is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: search index 'titles' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: display of vertex label 'Team' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: property schema of edge type 'OWNS' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: deprecation of vertex label 'Draft' is in the tenant but not declared here "
    "(added elsewhere, e.g. in the web UI)",
    "acme: display of vertex label 'Doc' would be replaced: it differs from the stored one",
    "acme: property schema of vertex label 'Doc' would be replaced: it differs from the "
    "stored one",
    "acme: meta key 'owner' would be replaced: it differs from the stored one",
    "acme: search index 'names' would be replaced: it differs from the stored one",
    "acme: edge type 'OLD' was dropped on 2026-10-20T09:30:00Z by olive; "
    "registering creates it again",
    "acme: label pair Doc-[LINKS_TO]->Doc was removed on 2026-10-21T10:00:00Z by olive; "
    "registering declares it again",
    "acme: vertex label 'Old' was dropped; registering creates it again",
]


class _Server:
    """Records registrations; answers a dry run with ``report`` as its drift, or
    every registration with ``refuse`` (status, body)."""

    def __init__(
        self, *, report: dict | None = None, refuse: tuple[int, dict] | None = None,
    ) -> None:
        self.requests: list[tuple[dict, dict]] = []
        self._report = report
        self._refuse = refuse

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        body = json.loads(request.content)
        params = dict(request.url.params)
        self.requests.append((body, params))
        if self._refuse is not None:
            return httpx.Response(self._refuse[0], json=self._refuse[1])
        if params.get("dry_run") == "true":
            return httpx.Response(200, json={"tenant": body["name"], "changes": [],
                                             "drift": self._report})
        return httpx.Response(200, json={"tenant": body["name"], "status": "updated"})

    @property
    def bodies(self) -> list[dict]:
        return [body for body, _ in self.requests]

    @property
    def params(self) -> list[dict]:
        return [params for _, params in self.requests]


def _client(server: _Server, **options: object) -> GraphosClient:
    options.setdefault("token", "tok")
    return GraphosClient(base_url="http://x", tenant="acme",
                         transport=httpx.MockTransport(server), **options)


def _drift_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _DRIFT_LOG]


# ── schema_manager read and sent ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_manager_is_read_from_the_tenant_and_the_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tenants/acme":
            return httpx.Response(200, json={"name": "acme", "schema_manager": "user:loader"})
        if request.url.path == "/v1/acme/schema":
            return httpx.Response(200, json={"name": "acme", "schema_manager": "user:loader"})
        return httpx.Response(200, json={"name": "other"})        # an older server: no field

    async with GraphosClient(base_url="http://x", tenant="acme", token="tok",
                             transport=httpx.MockTransport(handler)) as cli:
        assert (await cli.get_tenant()).schema_manager == "user:loader"
        assert (await cli.schema()).schema_manager == "user:loader"
        assert (await cli.get_tenant("other")).schema_manager is None
        assert (await cli.schema("other")).schema_manager is None


@pytest.mark.asyncio
async def test_register_sends_schema_manager_a_subject_null_or_nothing() -> None:
    server = _Server()
    async with _client(server) as cli:
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager="user:loader")
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager=None)
        await cli.register_tenant(vertex_labels=["Doc"])
    assert server.bodies[0]["schema_manager"] == "user:loader"
    assert "schema_manager" in server.bodies[1]          # JSON null: release
    assert server.bodies[1]["schema_manager"] is None
    assert "schema_manager" not in server.bodies[2]      # left out: unchanged


@pytest.mark.asyncio
async def test_import_schema_sends_schema_manager_only_when_given() -> None:
    server = _Server()
    # An exported document may name its tenant's manager; importing it does
    # not move the manager by itself.
    doc = {"name": "acme", "vertex_labels": ["Doc"], "schema_manager": "user:someone"}
    async with _client(server) as cli:
        await cli.import_schema(doc)
        await cli.import_schema(doc, schema_manager="user:loader")
        await cli.import_schema(doc, schema_manager=None)
    assert "schema_manager" not in server.bodies[0]
    assert server.bodies[1]["schema_manager"] == "user:loader"
    assert "schema_manager" in server.bodies[2] and server.bodies[2]["schema_manager"] is None


@pytest.mark.asyncio
async def test_manage_schema_sends_the_signed_in_user() -> None:
    server = _Server()
    async with _client(server, token=None, auth=PasswordAuth("loader", "secret")) as cli:
        await cli.register_tenant(vertex_labels=["Doc"], manage_schema=True)
    assert server.bodies[0]["schema_manager"] == "user:loader"


@pytest.mark.asyncio
async def test_manage_schema_uses_the_user_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHOS_USER", "loader")
    monkeypatch.setenv("GRAPHOS_PASSWORD", "secret")
    server = _Server()
    async with _client(server, token=None) as cli:
        await cli.register_tenant(vertex_labels=["Doc"], manage_schema=True)
    assert server.bodies[0]["schema_manager"] == "user:loader"


@pytest.mark.asyncio
async def test_manage_schema_without_a_known_user_name_is_refused() -> None:
    server = _Server()
    async with _client(server) as cli:                    # a bare token: no user name
        with pytest.raises(ValueError, match='schema_manager="user:<name>"'):
            await cli.register_tenant(vertex_labels=["Doc"], manage_schema=True)
        with pytest.raises(ValueError, match="not both"):
            await cli.register_tenant(manage_schema=True, schema_manager="user:loader")
        with pytest.raises(ValueError, match='schema_manager="user:<name>"'):
            await cli.ensure_tenant(vertex_labels=["Doc"], manage_schema=True)
    assert server.requests == []


@pytest.mark.asyncio
async def test_a_schema_managed_refusal_carries_its_code_and_manager() -> None:
    refusal = {"detail": "the schema of 'acme' is managed by user:loader",
               "code": "schema_managed", "schema_manager": "user:loader"}
    async with _client(_Server(refuse=(409, refusal))) as cli:
        with pytest.raises(GraphosError) as info:
            await cli.register_tenant(vertex_labels=["Doc"])
    err = info.value
    assert (err.status_code, err.code, err.schema_manager) == (409, "schema_managed",
                                                               "user:loader")
    assert err.detail == refusal["detail"] and err.body == refusal


def test_an_error_without_a_code_keeps_its_attributes() -> None:
    err = GraphosError(400, "bad", "POST", "/v1/tenants")
    assert (err.status_code, err.detail, err.body, err.code, err.schema_manager) == (
        400, "bad", None, None, None)


# ── ensure_tenant ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_tenant_warns_of_each_drift_item_then_registers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_DRIFT_LOG)
    server = _Server(report=_FULL_REPORT)
    async with _client(server) as cli:
        result = await cli.ensure_tenant(vertex_labels=["Doc"], edge_labels=["OLD"],
                                         schema_manager="user:loader")
    assert result == {"tenant": "acme", "status": "updated"}
    # The dry run and the registration send the same body.
    assert server.params == [{"dry_run": "true"}, {}]
    assert server.bodies[0] == server.bodies[1]
    assert server.bodies[0]["schema_manager"] == "user:loader"
    assert _drift_messages(caplog) == _FULL_LINES
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.name == _DRIFT_LOG)


@pytest.mark.asyncio
async def test_ensure_tenant_fail_raises_on_overwrites_and_registers_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_DRIFT_LOG)
    report = {"not_declared": {"vertex_labels": ["Team"]},
              "overwrites": {"display": {"vertex": ["Doc"]}}}
    server = _Server(report=report)
    async with _client(server) as cli:
        with pytest.raises(SchemaDriftError) as info:
            await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    err = info.value
    assert isinstance(err, GraphosError)
    assert err.drift == report and err.tenant == "acme"
    assert err.blocking == [
        "acme: display of vertex label 'Doc' would be replaced: it differs from the stored one",
    ]
    assert err.blocking[0] in str(err)
    assert len(err.lines) == 2 and _drift_messages(caplog) == err.lines
    assert server.params == [{"dry_run": "true"}]        # no registration


@pytest.mark.asyncio
async def test_ensure_tenant_fail_raises_on_recreates() -> None:
    report = {"recreates": [{"kind": "vertex_label", "name": "Old",
                             "removed_at": "2026-10-20T09:30:00Z", "removed_by": "olive"}]}
    server = _Server(report=report)
    async with _client(server) as cli:
        with pytest.raises(SchemaDriftError) as info:
            await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc", "Old"])
    assert info.value.drift == report
    assert info.value.blocking == [
        "acme: vertex label 'Old' was dropped on 2026-10-20T09:30:00Z by olive; "
        "registering creates it again",
    ]
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_ensure_tenant_fail_registers_when_items_are_only_not_declared(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_DRIFT_LOG)
    # Empty parts, as a server may send them, count as nothing.
    report = {"not_declared": {"vertex_labels": ["Team"], "edge_labels": []},
              "overwrites": {"display": {"vertex": [], "edge": []}, "meta": []},
              "recreates": []}
    server = _Server(report=report)
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    assert server.params == [{"dry_run": "true"}, {}]
    assert _drift_messages(caplog) == [
        "acme: vertex label 'Team' is in the tenant but not declared here "
        "(added elsewhere, e.g. in the web UI)",
    ]


@pytest.mark.asyncio
async def test_ensure_tenant_fail_counts_an_overwrite_of_a_part_it_does_not_know() -> None:
    # A part a newer server adds is still reported, and still fails.
    server = _Server(report={"overwrites": {"saved_queries": ["recent"]}})
    async with _client(server) as cli:
        with pytest.raises(SchemaDriftError) as info:
            await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    assert info.value.blocking == [
        "acme: saved queries 'recent' would be replaced: it differs from the stored one",
    ]


@pytest.mark.asyncio
async def test_ensure_tenant_on_a_new_tenant_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_DRIFT_LOG)
    server = _Server(report=None)                         # drift: null, a new tenant
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="fail", name="fresh", vertex_labels=["Doc"])
    assert [b["name"] for b in server.bodies] == ["fresh", "fresh"]
    assert _drift_messages(caplog) == []


@pytest.mark.asyncio
async def test_ensure_tenant_ignore_skips_the_dry_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_DRIFT_LOG)
    server = _Server(report=_FULL_REPORT)
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="ignore", vertex_labels=["Doc"])
    assert server.params == [{}]
    assert _drift_messages(caplog) == []


@pytest.mark.asyncio
async def test_ensure_tenant_reads_iterables_for_both_calls() -> None:
    server = _Server()
    async with _client(server) as cli:
        await cli.ensure_tenant(
            vertex_labels=(x for x in ["Team", "Doc"]),
            edge_endpoints={"OWNS": (p for p in [("Team", "Doc")])},
        )
    for body in server.bodies:
        assert body["vertex_labels"] == ["Team", "Doc"]
        assert body["edge_endpoints"] == {"OWNS": [["Team", "Doc"]]}


@pytest.mark.asyncio
async def test_ensure_tenant_manage_schema_sends_the_signed_in_user() -> None:
    server = _Server()
    async with _client(server, token=None, auth=PasswordAuth("loader", "secret")) as cli:
        await cli.ensure_tenant(vertex_labels=["Doc"], manage_schema=True)
    assert [b["schema_manager"] for b in server.bodies] == ["user:loader", "user:loader"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [
    {"detail": "the schema of 'acme' is managed by user:loader",
     "code": "schema_managed", "schema_manager": "user:loader"},
    {"detail": "vertex label 'Docu' was renamed to 'Doc'"},
])
async def test_ensure_tenant_a_refused_dry_run_raises_and_registers_nothing(
    refusal: dict,
) -> None:
    server = _Server(refuse=(409, refusal))
    async with _client(server) as cli:
        with pytest.raises(GraphosError) as info:
            await cli.ensure_tenant(on_drift="warn", vertex_labels=["Docu"])
    assert not isinstance(info.value, SchemaDriftError)
    assert info.value.status_code == 409
    assert info.value.code == refusal.get("code")
    assert server.params == [{"dry_run": "true"}]


@pytest.mark.asyncio
async def test_ensure_tenant_refuses_an_unknown_on_drift() -> None:
    server = _Server()
    async with _client(server) as cli:
        with pytest.raises(ValueError, match="on_drift"):
            await cli.ensure_tenant(on_drift="loud", vertex_labels=["Doc"])  # type: ignore[arg-type]
    assert server.requests == []
