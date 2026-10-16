"""The OpenAPI document describes every route fully and truthfully.

Each operation has a summary, a description, tags, described parameters and
a success response; a route whose body is not JSON (NDJSON, CSV) declares
what it takes; the shared error responses (``graphos.openapi``) are there;
and every status code a route raises, in its own code or in the helpers of
its module it calls, is declared.  The last check reads the source: a code
raised far down a call chain (a writer, a lookup) is not seen, so routes
still declare those by hand.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest
from fastapi.routing import APIRoute

from graphos.app import create_app
from graphos.config import Settings

METHODS = ("get", "put", "post", "patch", "delete")
_CODE = re.compile(r"HTTP_(\d{3})_")
# Bodies that are not JSON: the content types each route must declare.
RAW_BODIES = {
    ("post", "/v1/{tenant}/graph/nodes:bulk"): "application/x-ndjson",
    ("post", "/v1/{tenant}/graph/edges:bulk"): "application/x-ndjson",
    ("post", "/v1/{tenant}/graph/snapshot-import"): "application/x-ndjson",
    ("post", "/v1/{tenant}/graph/nodes:csv"): "text/csv",
    ("post", "/v1/{tenant}/graph/edges:csv"): "text/csv",
}
# POSTs that take no body at all.
NO_BODY = {
    ("post", "/v1/users/{name}:reset-secret"),
    ("post", "/v1/tenants/{name}/restore"),
    ("post", "/v1/jobs/{job_id}:cancel"),
}

_app = create_app(Settings(db_password="placeholder", auth_disabled=True))
SPEC = _app.openapi()
OPERATIONS = [
    (method, path, op)
    for path, item in SPEC["paths"].items()
    for method, op in item.items()
    if method in METHODS
]
OPERATION_IDS = [f"{m.upper()} {p}" for m, p, _ in OPERATIONS]


@pytest.mark.parametrize(("method", "path", "op"), OPERATIONS, ids=OPERATION_IDS)
def test_every_operation_is_described(method: str, path: str, op: dict[str, Any]) -> None:
    assert op.get("summary"), "no summary"
    assert (op.get("description") or "").strip(), "no description"
    assert op.get("tags"), "no tags"
    assert any(code.startswith("2") for code in op["responses"]), "no success response"
    for parameter in op.get("parameters", []):
        described = parameter.get("description") or parameter.get("schema", {}).get("description")
        assert described, f"parameter {parameter['in']}:{parameter['name']} has no description"
    for code, response in op["responses"].items():
        assert response.get("description"), f"response {code} has no description"
        if code[0] in "45":
            assert response.get("content"), f"error {code} has no body schema"


@pytest.mark.parametrize(("method", "path", "op"), OPERATIONS, ids=OPERATION_IDS)
def test_bodies_and_shared_errors_are_declared(method: str, path: str, op: dict[str, Any]) -> None:
    if method in ("post", "put", "patch") and (method, path) not in NO_BODY:
        assert "requestBody" in op, "no request body declared"
    if (method, path) in RAW_BODIES:
        assert RAW_BODIES[(method, path)] in op["requestBody"]["content"]
    if op.get("security"):
        assert {"401", "403"} <= set(op["responses"])
    if "{tenant}" in path:
        assert "404" in op["responses"]


def _routes(routes: list[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _routes(inner.routes)


def _raised(func: Callable[..., Any], module: ModuleType, depth: int = 0) -> set[str]:
    """Error codes ``func`` raises, and the same-module helpers it calls (two levels)."""
    source = textwrap.dedent(inspect.getsource(func))
    codes = {code for code in _CODE.findall(source) if code[0] in "45"}
    if depth < 2:  # noqa: PLR2004 — the helpers' helpers, no further
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = getattr(module, node.func.id, None)
                if inspect.isfunction(helper) and helper.__module__ == module.__name__:
                    codes |= _raised(helper, module, depth + 1)
    return codes


def _operation(route: APIRoute) -> tuple[str, str, dict[str, Any]] | None:
    method = sorted(route.methods)[0].lower()
    for prefix in ("", "/v1"):
        path = prefix + route.path_format
        op = SPEC["paths"].get(path, {}).get(method)
        if op is not None:
            return method, path, op
    return None


ROUTES = [r for r in _routes(_app.routes) if r.include_in_schema and _operation(r)]
ROUTE_IDS = [f"{sorted(r.methods)[0]} {r.path_format}" for r in ROUTES]


@pytest.mark.parametrize("route", ROUTES, ids=ROUTE_IDS)
def test_every_code_a_route_raises_is_declared(route: APIRoute) -> None:
    found = _operation(route)
    assert found is not None
    _, _, op = found
    raised = _raised(route.endpoint, sys.modules[route.endpoint.__module__])
    undeclared = sorted(raised - set(op["responses"]))
    assert undeclared == [], f"raises {undeclared} without declaring them"


def test_every_route_is_checked() -> None:
    assert len(ROUTES) == len(OPERATIONS)
