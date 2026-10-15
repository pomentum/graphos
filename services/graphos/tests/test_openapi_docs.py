"""The OpenAPI schema documents every operation (no DB required).

Each operation has a summary, a description and a tag, and each of its
parameters a description, so ``/docs`` and ``/redoc`` leave nothing
unexplained.  Read from the schema, the public contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from graphos.app import create_app
from graphos.config import Settings

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, cypher=True))
    return app.openapi()


def _operations(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{method.upper()} {path}", op)
        for path, ops in schema["paths"].items()
        for method, op in ops.items() if method in METHODS
    ]


def test_every_operation_has_a_summary_a_description_and_a_tag(
    schema: dict[str, Any],
) -> None:
    missing = [
        f"{name}: {part}"
        for name, op in _operations(schema)
        for part in ("summary", "description", "tags")
        if not op.get(part) or (part != "tags" and not op[part].strip())
    ]
    assert missing == []


def test_every_parameter_has_a_description(schema: dict[str, Any]) -> None:
    missing = [
        f"{name}: {param['in']} {param['name']}"
        for name, op in _operations(schema)
        for param in op.get("parameters", [])
        if not (param.get("description") or "").strip()
    ]
    assert missing == []


def test_the_root_links_are_left_out(schema: dict[str, Any]) -> None:
    assert "/" not in schema["paths"]
