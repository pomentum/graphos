"""A tenant's schema manager and the drift report, over a mock transport.

``schema_manager`` is read from the tenant and the schema, sent with a
registration or an import, and checked in the answer; ``display_merge`` is
sent when given; ``ensure_tenant`` runs a registration's dry run, logs its
``drift`` and registers with the dry run's ``ETag`` (or fails, or skips the
dry run).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from graphos_client import (
    GraphosClient,
    GraphosError,
    PasswordAuth,
    SchemaDriftError,
    SchemaManagerNotApplied,
)

_DRIFT_LOG = "graphos_client.drift"
_UNDECLARED = "is in the tenant but not declared here (added elsewhere, e.g. in the web UI)"
_REMOVED = "would be removed: the registration replaces the schema and does not declare it"
_REPLACED = "would be replaced: it differs from the stored one"

# Every part of a drift report, one item each (a real report has either
# ``not_declared``, for a merge, or ``removes``, for a replace).
_FULL_REPORT: dict[str, Any] = {
    "not_declared": {
        "vertex_labels": ["Team"],
        "edge_labels": ["MENTIONS"],
        "edge_endpoints": [["OWNS", "Team", "Doc"]],
        "label_hierarchy": [["Memo", "Doc"]],
        "search_indexes": ["titles"],
        "resolve_rules": ["CITES"],
        "property_indexes": ["Doc(kind)"],
        "generated_columns": ["Doc.kind (text, from kind)"],
        "meta": ["team"],
        "display": {"vertex": ["Team"], "edge": []},
        "property_schema": {"vertex": [], "edge": ["OWNS"]},
        "deprecated": {"vertex": ["Draft"], "edge": []},
    },
    "removes": {
        "vertex_labels": ["Memo"],
        "edge_endpoints": [["CITES", "Doc", "Doc"]],
        "display": {"edge": ["CITES"]},
    },
    "overwrites": {
        "display": {"vertex": ["Doc"], "edge": []},
        "property_schema": {"vertex": ["Doc"]},
        "meta": ["owner"],
        "search_indexes": ["names"],
    },
    "settings": {
        "strict_endpoints": [False, True],
        "property_mode": ["warn", "strict"],
        "community_algorithm": ["louvain", "leiden"],
    },
    "recreates": {
        "vertex_labels": [{"name": "Old", "removed_at": None, "removed_by": None}],
        "edge_labels": [{"name": "OLD", "removed_at": "2026-10-20T09:30:00Z",
                         "removed_by": "olive"}],
        "edge_endpoints": [{"pair": ["LINKS_TO", "Doc", "Doc"],
                            "removed_at": "2026-10-21T10:00:00Z", "removed_by": "olive"}],
    },
}

_INFO_LINES = [
    f"acme: vertex label 'Team' {_UNDECLARED}",
    f"acme: edge type 'MENTIONS' {_UNDECLARED}",
    f"acme: label pair Team-[OWNS]->Doc {_UNDECLARED}",
    f"acme: label hierarchy 'Memo' under 'Doc' {_UNDECLARED}",
    f"acme: search index 'titles' {_UNDECLARED}",
    f"acme: resolve rule of edge type 'CITES' {_UNDECLARED}",
    f"acme: property index 'Doc(kind)' {_UNDECLARED}",
    f"acme: generated column 'Doc.kind (text, from kind)' {_UNDECLARED}",
    f"acme: meta key 'team' {_UNDECLARED}",
    f"acme: display of vertex label 'Team' {_UNDECLARED}",
    f"acme: property schema of edge type 'OWNS' {_UNDECLARED}",
    f"acme: deprecation of vertex label 'Draft' {_UNDECLARED}",
]

_WARNING_LINES = [
    f"acme: vertex label 'Memo' {_REMOVED}",
    f"acme: label pair Doc-[CITES]->Doc {_REMOVED}",
    f"acme: display of edge type 'CITES' {_REMOVED}",
    f"acme: display of vertex label 'Doc' {_REPLACED}",
    f"acme: property schema of vertex label 'Doc' {_REPLACED}",
    f"acme: meta key 'owner' {_REPLACED}",
    f"acme: search index 'names' {_REPLACED}",
    "acme: strict_endpoints would change from off to on",
    "acme: the property mode would change from 'warn' to 'strict'",
    "acme: the community algorithm would change from 'louvain' to 'leiden'",
    "acme: vertex label 'Old' was dropped; registering creates it again",
    "acme: edge type 'OLD' was dropped on 2026-10-20T09:30:00Z by olive; "
    "registering creates it again",
    "acme: label pair Doc-[LINKS_TO]->Doc was removed on 2026-10-21T10:00:00Z by olive; "
    "registering declares it again",
]

_OMIT: Any = object()


class _Server:
    """Records registrations.  A dry run answers ``report`` as its drift and
    ``etag`` in its ``ETag`` header; a registration answers the manager it
    stores (``"self"`` is ``user:loader``), or ``answer_manager`` instead
    (``_OMIT``: no ``schema_manager`` at all, an older server).  ``refuse``
    (status, body) answers every registration."""

    def __init__(
        self, *, report: dict | None = None, refuse: tuple[int, dict] | None = None,
        etag: str | None = '"v7"', answer_manager: Any = None, older: bool = False,
    ) -> None:
        self.requests: list[tuple[dict, dict, str | None]] = []
        self._report = report
        self._refuse = refuse
        self._etag = etag
        self._answer = _OMIT if older else answer_manager
        self._override = older or answer_manager is not None
        self.manager: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        body = json.loads(request.content)
        params = dict(request.url.params)
        self.requests.append((body, params, request.headers.get("if-match")))
        if self._refuse is not None:
            return httpx.Response(self._refuse[0], json=self._refuse[1])
        if params.get("dry_run") == "true":
            headers = {"ETag": self._etag} if self._etag else {}
            return httpx.Response(200, headers=headers, json={
                "tenant": body["name"], "changes": None, "drift": self._report,
            })
        if "schema_manager" in body:
            wanted = body["schema_manager"]
            self.manager = "user:loader" if wanted == "self" else wanted
        answer: dict[str, Any] = {"tenant": body["name"], "status": "bootstrapped"}
        if not self._override:
            answer["schema_manager"] = self.manager
        elif self._answer is not _OMIT:
            answer["schema_manager"] = self._answer
        return httpx.Response(200, json=answer, headers={"ETag": '"v8"'})

    @property
    def bodies(self) -> list[dict]:
        return [body for body, _, _ in self.requests]

    @property
    def params(self) -> list[dict]:
        return [params for _, params, _ in self.requests]

    @property
    def if_match(self) -> list[str | None]:
        return [header for _, _, header in self.requests]


def _client(server: _Server, **options: object) -> GraphosClient:
    options.setdefault("token", "tok")
    return GraphosClient(base_url="http://x", tenant="acme",
                         transport=httpx.MockTransport(server), **options)


def _drift_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _DRIFT_LOG]


def _drift_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _drift_records(caplog)]


# ── schema_manager read and sent ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_manager_is_read_from_the_tenant_and_the_schema() -> None:
    managed = {"schema_manager": "user:loader", "schema_manager_since": "2026-10-29T08:00:00Z"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tenants/acme":
            return httpx.Response(200, json={"name": "acme", **managed})
        if request.url.path == "/v1/acme/schema":
            return httpx.Response(200, json={"name": "acme", **managed})
        return httpx.Response(200, json={"name": "other"})        # an older server: no field

    since = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
    async with GraphosClient(base_url="http://x", tenant="acme", token="tok",
                             transport=httpx.MockTransport(handler)) as cli:
        tenant, schema = await cli.get_tenant(), await cli.schema()
        assert (tenant.schema_manager, tenant.schema_manager_since) == ("user:loader", since)
        assert (schema.schema_manager, schema.schema_manager_since) == ("user:loader", since)
        other = await cli.get_tenant("other")
        assert (other.schema_manager, other.schema_manager_since) == (None, None)
        assert (await cli.schema("other")).schema_manager is None


@pytest.mark.asyncio
async def test_register_sends_schema_manager_a_subject_null_or_nothing() -> None:
    server = _Server()
    async with _client(server) as cli:
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager="user:loader")
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager="oidc-sub:abc-123")
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager=None)
        await cli.register_tenant(vertex_labels=["Doc"])
    assert server.bodies[0]["schema_manager"] == "user:loader"
    assert server.bodies[1]["schema_manager"] == "oidc-sub:abc-123"
    assert "schema_manager" in server.bodies[2]          # JSON null: release
    assert server.bodies[2]["schema_manager"] is None
    assert "schema_manager" not in server.bodies[3]      # left out: unchanged


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
@pytest.mark.parametrize("sign_in", [
    {"token": "tok"},                                      # a bare token: no user name known
    {"token": None, "auth": PasswordAuth("loader", "secret")},
])
async def test_manage_schema_sends_self_whatever_the_sign_in(sign_in: dict) -> None:
    server = _Server()
    async with _client(server, **sign_in) as cli:
        result = await cli.register_tenant(vertex_labels=["Doc"], manage_schema=True)
        await cli.ensure_tenant(vertex_labels=["Doc"], manage_schema=True)
        await cli.import_schema({"vertex_labels": ["Doc"]}, schema_manager="self")
    assert [b["schema_manager"] for b in server.bodies] == ["self"] * 4
    assert result["schema_manager"] == "user:loader"


@pytest.mark.asyncio
async def test_manage_schema_and_schema_manager_together_are_refused() -> None:
    server = _Server()
    async with _client(server) as cli:
        with pytest.raises(ValueError, match="not both"):
            await cli.register_tenant(manage_schema=True, schema_manager="user:loader")
        with pytest.raises(ValueError, match="not both"):
            await cli.ensure_tenant(manage_schema=True, schema_manager=None)
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


# ── display_merge ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_display_merge_is_sent_when_given_and_left_out_otherwise() -> None:
    server = _Server()
    display = {"vertex": {"Doc": {"color": "#5FA584"}}}
    async with _client(server) as cli:
        await cli.register_tenant(vertex_labels=["Doc"], display=display,
                                  display_merge="entries")
        await cli.register_tenant(vertex_labels=["Doc"], display=display)
        await cli.import_schema({"vertex_labels": ["Doc"]}, display_merge="missing_only")
        await cli.import_schema({"vertex_labels": ["Doc"]})
        await cli.ensure_tenant(vertex_labels=["Doc"], display_merge="entries")
    merges = [b.get("display_merge", "-") for b in server.bodies]
    # The last two are ensure_tenant's dry run and registration.
    assert merges == ["entries", "-", "missing_only", "-", "entries", "entries"]


# ── the manager checked in the answer (an older server ignores it) ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "answered"), [
    ("user:loader", "user:loader"),
    ("oidc-sub:abc-123", "oidc-sub:abc-123"),
    ("self", "user:loader"),
    ("self", "oidc-sub:abc-123"),
    (None, None),
])
async def test_a_registration_whose_answer_names_the_manager_asked_passes(
    sent: str | None, answered: str | None,
) -> None:
    server = _Server(answer_manager=answered) if answered else _Server()
    async with _client(server) as cli:
        result = await cli.register_tenant(vertex_labels=["Doc"], schema_manager=sent)
    assert result["schema_manager"] == answered


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["user:loader", "self", None])
async def test_an_answer_without_schema_manager_raises_not_applied(sent: str | None) -> None:
    server = _Server(older=True)
    async with _client(server) as cli:
        with pytest.raises(SchemaManagerNotApplied) as info:
            await cli.register_tenant(vertex_labels=["Doc"], schema_manager=sent)
    err = info.value
    assert isinstance(err, GraphosError) and err.status_code == 0
    assert (err.tenant, err.requested, err.answered) == ("acme", sent, None)
    assert "did not apply" in str(err) and "older" in str(err)
    assert err.result["status"] == "bootstrapped"
    assert len(server.requests) == 1                      # the registration was made


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "answered"), [
    ("user:loader", "user:someone"),
    ("user:loader", None),
    (None, "user:loader"),
])
async def test_an_answer_naming_another_manager_raises_not_applied(
    sent: str | None, answered: str | None,
) -> None:
    class _Other(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.requests.append((body, dict(request.url.params), None))
            return httpx.Response(200, json={"tenant": "acme", "schema_manager": answered})

    async with _client(_Other()) as cli:
        with pytest.raises(SchemaManagerNotApplied) as info:
            await cli.register_tenant(vertex_labels=["Doc"], schema_manager=sent)
    assert (info.value.requested, info.value.answered) == (sent, answered)
    assert f"schema_manager={answered!r}" in str(info.value)


@pytest.mark.asyncio
async def test_self_answered_with_no_manager_raises_not_applied() -> None:
    class _NoManager(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append((json.loads(request.content), {}, None))
            return httpx.Response(200, json={"tenant": "acme", "schema_manager": None})

    async with _client(_NoManager()) as cli:
        with pytest.raises(SchemaManagerNotApplied):
            await cli.register_tenant(vertex_labels=["Doc"], manage_schema=True)


@pytest.mark.asyncio
async def test_no_check_without_a_manager_or_on_a_dry_run() -> None:
    server = _Server(older=True)
    async with _client(server) as cli:
        await cli.register_tenant(vertex_labels=["Doc"])
        await cli.register_tenant(vertex_labels=["Doc"], schema_manager="self", dry_run=True)
        await cli.import_schema({"vertex_labels": ["Doc"]}, schema_manager="self", dry_run=True)
        with pytest.raises(SchemaManagerNotApplied):
            await cli.import_schema({"vertex_labels": ["Doc"]}, schema_manager="self")
        with pytest.raises(SchemaManagerNotApplied):
            await cli.ensure_tenant(vertex_labels=["Doc"], manage_schema=True)
    # ensure_tenant registered after its dry run, then found the manager missing.
    assert server.params[-2:] == [{"dry_run": "true"}, {}]


# ── import_schema with more than 5,000 pairs ───────────────────────────────


@pytest.mark.asyncio
async def test_import_sends_schema_manager_with_its_last_call_only() -> None:
    server = _Server()
    pairs = [[f"L{i}", "Doc"] for i in range(10_001)]
    doc = {"name": "acme", "vertex_labels": ["Doc"], "edge_endpoints": {"R": pairs},
           "display": {"vertex": {"Doc": {"color": "#5FA584"}}}}
    async with _client(server) as cli:
        result = await cli.import_schema(doc, schema_manager="self", display_merge="entries",
                                         if_match='"v1"')
    assert len(server.requests) == 3
    assert ["schema_manager" in b for b in server.bodies] == [False, False, True]
    assert server.bodies[2]["schema_manager"] == "self"
    # display_merge and If-Match go with the first call, the rest of the schema.
    assert ["display_merge" in b for b in server.bodies] == [True, False, False]
    assert server.if_match == ['"v1"', None, None]
    assert result["schema_manager"] == "user:loader"

    # A dry run is one call, and carries the manager.
    server.requests.clear()
    async with _client(server) as cli:
        await cli.import_schema(doc, schema_manager="user:loader", dry_run=True)
    assert len(server.requests) == 1 and server.bodies[0]["schema_manager"] == "user:loader"


@pytest.mark.asyncio
async def test_import_checks_the_manager_in_the_last_answer() -> None:
    server = _Server(older=True)
    doc = {"vertex_labels": ["Doc"], "edge_endpoints": {"R": [["A", "B"]] * 5001}}
    async with _client(server) as cli:
        with pytest.raises(SchemaManagerNotApplied):
            await cli.import_schema(doc, schema_manager="user:loader")
    assert len(server.requests) == 2                      # every pair was sent


# ── ensure_tenant ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_tenant_logs_each_drift_item_at_its_level_then_registers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    server = _Server(report=_FULL_REPORT)
    async with _client(server) as cli:
        result = await cli.ensure_tenant(vertex_labels=["Doc"], edge_labels=["OLD"],
                                         schema_manager="user:loader")
    assert result == {"tenant": "acme", "status": "bootstrapped",
                      "schema_manager": "user:loader", "etag": '"v8"'}
    # The dry run and the registration send the same body.
    assert server.params == [{"dry_run": "true"}, {}]
    assert server.bodies[0] == server.bodies[1]
    assert server.bodies[0]["schema_manager"] == "user:loader"
    records = _drift_records(caplog)
    assert [r.getMessage() for r in records] == _INFO_LINES + _WARNING_LINES
    assert [r.levelno for r in records] == (
        [logging.INFO] * len(_INFO_LINES) + [logging.WARNING] * len(_WARNING_LINES))


@pytest.mark.asyncio
async def test_ensure_tenant_registers_with_the_dry_runs_etag_as_if_match() -> None:
    server = _Server(report={"not_declared": {"vertex_labels": ["Team"]}})
    async with _client(server) as cli:
        await cli.ensure_tenant(vertex_labels=["Doc"])
        # The dry run is held to the caller's if_match; the registration to
        # the version the dry run judged.
        await cli.ensure_tenant(vertex_labels=["Doc"], if_match='"v6"')
    assert server.if_match == [None, '"v7"', '"v6"', '"v7"']


@pytest.mark.asyncio
async def test_ensure_tenant_without_an_etag_registers_without_if_match() -> None:
    server = _Server(etag=None)                          # a new tenant, or an older server
    async with _client(server) as cli:
        await cli.ensure_tenant(name="fresh", vertex_labels=["Doc"])
        await cli.ensure_tenant(vertex_labels=["Doc"], if_match='"v6"')
    assert server.if_match == [None, None, '"v6"', '"v6"']


@pytest.mark.asyncio
async def test_ensure_tenant_judges_once_more_when_another_start_moved_the_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _MovedOnce(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-match") == '"v7"' and self._etag == '"v7"':
                self.requests.append((json.loads(request.content), {}, '"v7"'))
                self._etag = '"v9"'                       # another replica registered
                return httpx.Response(412, json={"detail": "If-Match failed"},
                                      headers={"ETag": '"v9"'})
            return super().__call__(request)

    server = _MovedOnce(report={"not_declared": {"vertex_labels": ["Team"]}})
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    async with _client(server) as cli:
        result = await cli.ensure_tenant(vertex_labels=["Doc"])
    assert result["status"] == "bootstrapped"
    # The second dry run's report is the same: it is not logged twice.
    assert len(_drift_records(caplog)) == 1
    # dry run, refused registration, dry run on the new version, registration with it.
    assert [r[2] for r in server.requests] == [None, '"v7"', None, '"v9"']


@pytest.mark.asyncio
async def test_ensure_tenant_never_restores_a_tenant_unregistered_meanwhile() -> None:
    class _Unregistered(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.headers.get("if-match") == '"v7"':
                self.requests.append((body, {}, '"v7"'))
                self._gone = True                         # someone unregistered it
                return httpx.Response(412, json={"detail": "If-Match failed"})
            if getattr(self, "_gone", False):
                self.requests.append((body, dict(request.url.params), None))
                return httpx.Response(200, json={"tenant": body["name"], "restores": True,
                                                 "drift": None})
            return super().__call__(request)

    server = _Unregistered()
    async with _client(server) as cli:
        with pytest.raises(GraphosError) as info:
            await cli.ensure_tenant(vertex_labels=["Doc"])
    assert info.value.status_code == 412
    assert "unregistered meanwhile" in info.value.detail
    # Dry run, refused registration, dry run that would restore: no restoring registration.
    assert len(server.requests) == 3


@pytest.mark.asyncio
async def test_ensure_tenant_raises_412_when_the_schema_changed_after_the_dry_run() -> None:
    class _Moved(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-match") == '"v7"':
                self.requests.append((json.loads(request.content), {}, '"v7"'))
                return httpx.Response(412, json={"detail": "If-Match failed"},
                                      headers={"ETag": '"v9"'})
            return super().__call__(request)

    server = _Moved()
    async with _client(server) as cli:
        with pytest.raises(GraphosError) as info:
            await cli.ensure_tenant(vertex_labels=["Doc"])
    assert info.value.status_code == 412
    # Judged and refused twice: it gives up after the second.
    assert len(server.requests) == 4


@pytest.mark.asyncio
async def test_a_dry_runs_answer_carries_its_etag() -> None:
    server = _Server()
    async with _client(server) as cli:
        preview = await cli.register_tenant(vertex_labels=["Doc"], dry_run=True)
    assert preview["etag"] == '"v7"'


@pytest.mark.asyncio
@pytest.mark.parametrize(("report", "blocking"), [
    ({"not_declared": {"vertex_labels": ["Team"]},
      "overwrites": {"display": {"vertex": ["Doc"]}}},
     [f"acme: display of vertex label 'Doc' {_REPLACED}"]),
    ({"removes": {"vertex_labels": ["Memo"], "label_hierarchy": [["Memo", "Doc"]]}},
     [f"acme: vertex label 'Memo' {_REMOVED}",
      f"acme: label hierarchy 'Memo' under 'Doc' {_REMOVED}"]),
    ({"settings": {"strict_endpoints": [False, True]}},
     ["acme: strict_endpoints would change from off to on"]),
    ({"recreates": {"vertex_labels": [{"name": "Old", "removed_at": "2026-10-20T09:30:00Z",
                                       "removed_by": "olive"}]}},
     ["acme: vertex label 'Old' was dropped on 2026-10-20T09:30:00Z by olive; "
      "registering creates it again"]),
])
async def test_ensure_tenant_fail_raises_and_registers_nothing(
    caplog: pytest.LogCaptureFixture, report: dict, blocking: list[str],
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    server = _Server(report=report)
    async with _client(server) as cli:
        with pytest.raises(SchemaDriftError) as info:
            await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    err = info.value
    assert isinstance(err, GraphosError)
    assert err.drift == report and err.tenant == "acme"
    assert err.blocking == blocking
    assert all(line in str(err) for line in blocking)
    assert _drift_messages(caplog) == err.lines
    assert server.params == [{"dry_run": "true"}]        # no registration


@pytest.mark.asyncio
async def test_ensure_tenant_fail_registers_when_items_are_only_not_declared(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    # Empty and null parts, lists and fields, as a server may send them, count as nothing.
    report = {"not_declared": {"vertex_labels": ["Team"], "edge_labels": [], "meta": None},
              "removes": None,
              "overwrites": {"display": {"vertex": [], "edge": None}, "meta": []},
              "settings": {},
              "recreates": {"vertex_labels": [], "edge_endpoints": None}}
    server = _Server(report=report)
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    assert server.params == [{"dry_run": "true"}, {}]
    records = _drift_records(caplog)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, f"acme: vertex label 'Team' {_UNDECLARED}"),
    ]


@pytest.mark.asyncio
async def test_ensure_tenant_fail_counts_an_overwrite_of_a_part_it_does_not_know() -> None:
    # A part a newer server adds is still reported, and still fails.
    server = _Server(report={"overwrites": {"saved_queries": ["recent"]}})
    async with _client(server) as cli:
        with pytest.raises(SchemaDriftError) as info:
            await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    assert info.value.blocking == [f"acme: saved queries 'recent' {_REPLACED}"]


@pytest.mark.asyncio
async def test_ensure_tenant_warns_of_a_section_it_does_not_know_without_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    server = _Server(report={"renamed": {"vertex_labels": ["Docu"]}})
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="fail", vertex_labels=["Doc"])
    assert server.params == [{"dry_run": "true"}, {}]
    records = _drift_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "renamed" in records[0].getMessage() and "Docu" in records[0].getMessage()


@pytest.mark.asyncio
async def test_ensure_tenant_on_a_new_tenant_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    server = _Server(report=None)                         # drift: null, a new tenant
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="fail", name="fresh", vertex_labels=["Doc"])
    assert [b["name"] for b in server.bodies] == ["fresh", "fresh"]
    assert _drift_messages(caplog) == []


@pytest.mark.asyncio
async def test_ensure_tenant_ignore_skips_the_dry_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_DRIFT_LOG)
    server = _Server(report=_FULL_REPORT)
    async with _client(server) as cli:
        await cli.ensure_tenant(on_drift="ignore", vertex_labels=["Doc"], if_match='"v6"')
    assert server.params == [{}]
    assert server.if_match == ['"v6"']
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


@pytest.mark.asyncio
async def test_ensure_tenant_creates_only_what_it_judged_absent() -> None:
    for restores, expected in ((False, "*"), (True, None)):
        seen: list[tuple[str | None, str | None]] = []

        def handler(request: httpx.Request, restores: bool = restores,
                    seen: list[tuple[str | None, str | None]] = seen) -> httpx.Response:
            seen.append((request.headers.get("if-match"), request.headers.get("if-none-match")))
            if request.url.params.get("dry_run") == "true":
                return httpx.Response(200, json={"tenant": "fresh", "exists": False,
                                                 "restores": restores, "drift": None})
            return httpx.Response(200, json={"tenant": "fresh", "status": "bootstrapped"})

        async with GraphosClient(base_url="http://x", tenant="fresh", token="tok",
                                 transport=httpx.MockTransport(handler)) as cli:
            await cli.ensure_tenant(vertex_labels=["Doc"])
        # A new tenant is created with If-None-Match: *; one it restores (as the dry
        # run said) is not, which graphos would refuse.
        assert seen == [(None, None), (None, expected)]
