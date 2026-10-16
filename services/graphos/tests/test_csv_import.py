"""``POST /v1/{tenant}/graph/nodes:csv`` and ``…/edges:csv`` without a database.

The tenant registry and the SQL writers are replaced: these tests check what
the routes read, refuse and hand to the writers; ``test_csv_import_db.py``
writes for real.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

import pytest
from fastapi.testclient import TestClient

from graphos import lookup, writes
from graphos.api import csv_import, graph
from graphos.app import create_app
from graphos.bootstrap import TenantSpec
from graphos.config import Settings

NODES = "/v1/acme/graph/nodes:csv"
EDGES = "/v1/acme/graph/edges:csv"
CSV = {"Content-Type": "text/csv"}


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[dict[str, Any]]]]:
    spec = TenantSpec(
        name="acme", vertex_labels=("Person", "Team"), edge_labels=("MEMBER_OF", "KNOWS"),
        edge_endpoints=(("MEMBER_OF", "Person", "Team"),), strict_endpoints=True,
        property_schema={"vertex": {"Person": {
            "mode": "strict",
            "properties": {
                "name": {"type": "string", "required": True}, "age": {"type": "integer"},
            },
        }}},
    )
    for module in (graph, csv_import):
        monkeypatch.setattr(module, "registered_tenants", lambda: {"acme": spec})
    monkeypatch.setattr("graphos.api.bulk.registered_tenants", lambda: {"acme": spec})
    seen: list[tuple[str, list[dict[str, Any]]]] = []

    async def upsert_nodes(_pool: object, *, tenant: str, label: str, rows: list) -> int:
        seen.append((label, rows))
        return len(rows)

    async def upsert_edges(
        _pool: object, *, tenant: str, edge_type: str, rows: list, **_: Any,
    ) -> tuple[int, int]:
        seen.append((edge_type, rows))
        return len(rows) - 1, 1

    async def existing_ids(_conn: object, _tenant: str, _label: str, ids: list[str]) -> set[str]:
        return {i for i in ids if i != "ghost"}

    monkeypatch.setattr(writes, "upsert_nodes", upsert_nodes)
    monkeypatch.setattr(writes, "upsert_edges", upsert_edges)
    monkeypatch.setattr(lookup, "existing_ids", existing_ids)
    return seen


class _Pool:
    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, csv_max_bytes=4096))
    app.state.pool = _Pool()
    return TestClient(app, raise_server_exceptions=False)


def test_imports_nodes_merged_typed_and_checked(client: TestClient, written: list) -> None:
    body = "id;name;age;notes\n a ;Ann;30;x\nb;;41;\na;Annie;;\nc;Cy;old;\n;Nobody;1;\n"
    resp = client.post(NODES, params={"label": "Person"}, content=body, headers=CSV)
    assert resp.status_code == HTTPStatus.OK, resp.text
    answer = resp.json()
    assert (answer["delimiter"], answer["rows"], answer["records"]) == ("semicolon", 5, 4)
    assert (answer["written"], answer["rows_invalid"], answer["dry_run"]) == (2, 2, False)
    assert [c["property"] for c in answer["columns"]] == ["name", "age", "notes"]
    assert answer["columns"][1]["type"] == "number"
    assert written == [("Person", [
        {"id": "a", "props": {"name": "Annie", "age": 30, "notes": "x"}},
        {"id": "c", "props": {"name": "Cy"}},
    ])]
    # b lacks the required name (strict); the keyless row is refused for its id.
    assert answer["errors"][0].startswith("row 2: property_schema: ")
    assert answer["errors"][1].startswith("row 5: id:")
    codes = {i["code"]: i for i in answer["issues"]}
    assert codes["empty_id"]["refused"] is True
    assert codes["duplicate_id"]["examples"] == [{"row": 3, "value": "a"}]
    assert codes["bad_value"]["examples"] == [{"row": 4, "column": "age", "value": "old"}]
    assert answer["property_violations"] == {"Person.name:required": 1}


def test_a_dry_run_writes_nothing(client: TestClient, written: list) -> None:
    resp = client.post(
        NODES, params={"label": "Person", "dry_run": "true"}, content="id,name\na,Ann\nb,Bo\n",
        headers=CSV,
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert (resp.json()["written"], resp.json()["dry_run"]) == (2, True)
    assert written == []


def test_maps_columns_as_told(client: TestClient, written: list) -> None:
    columns = {
        "Full name": {"property": "name"}, "Age": {"property": None}, "code": {"type": "text"},
    }
    resp = client.post(
        NODES,
        params={"label": "Person", "id": "Key", "columns": json.dumps(columns)},
        content="Full name,Key,Age,code\nAnn,k1,30,007\n", headers=CSV,
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert written == [("Person", [{"id": "k1", "props": {"name": "Ann", "code": "007"}}])]


@pytest.mark.parametrize(
    ("params", "body", "status"),
    [
        ({"label": "Person", "columns": "{"}, "id\na\n", HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"label": "Person", "columns": '{"x": 1}'}, "id\na\n", HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"label": "Person", "id": "nope"}, "id\na\n", HTTPStatus.UNPROCESSABLE_ENTITY),
        (
            {"label": "Person", "columns": '{"n": {"property": "graphos_x"}}'},
            "id,n\na,1\n", HTTPStatus.UNPROCESSABLE_ENTITY,
        ),
        ({"label": "Nope"}, "id\na\n", HTTPStatus.BAD_REQUEST),
        ({"label": "Person"}, "", HTTPStatus.BAD_REQUEST),
        ({"label": "Person"}, 'id,n\na,"open\n', HTTPStatus.BAD_REQUEST),
        ({"label": "Person"}, "id\n" + "a\n" * 3000, HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    ],
)
def test_refuses_a_request_before_writing(
    client: TestClient, written: list, params: dict, body: str, status: int,
) -> None:
    resp = client.post(NODES, params=params, content=body, headers=CSV)
    assert resp.status_code == status, resp.text
    assert written == []


def test_imports_relationships_and_counts_missing_ends(client: TestClient, written: list) -> None:
    body = "from,to,since\np1,t1,2020\np1,t1,2021\np2,ghost,\n,t1,1\n"
    params = {"type": "MEMBER_OF", "src_label": "Person", "dst_label": "Team"}
    resp = client.post(EDGES, params=params, content=body, headers=CSV)
    assert resp.status_code == HTTPStatus.OK, resp.text
    answer = resp.json()
    assert (answer["records"], answer["written"], answer["edges_skipped"]) == (3, 1, 1)
    assert written == [("MEMBER_OF", [
        {"src_id": "p1", "dst_id": "t1", "props": {"since": 2021}},
        {"src_id": "p2", "dst_id": "ghost", "props": {}},
    ])]
    assert {i["code"] for i in answer["issues"]} == {"empty_id", "duplicate_pair"}

    dry = client.post(EDGES, params={**params, "dry_run": "true"}, content=body, headers=CSV)
    assert (dry.json()["written"], dry.json()["edges_skipped"]) == (1, 1)
    assert len(written) == 1


def test_refuses_an_undeclared_pair_when_strict(client: TestClient, written: list) -> None:
    params = {"type": "MEMBER_OF", "src_label": "Team", "dst_label": "Person"}
    resp = client.post(EDGES, params=params, content="src,dst\na,b\n", headers=CSV)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "not declared" in resp.json()["detail"]
    resp = client.post(
        EDGES, params={"type": "NOPE", "src_label": "Team", "dst_label": "Person"},
        content="src,dst\na,b\n", headers=CSV,
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert written == []
