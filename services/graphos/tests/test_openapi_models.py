"""Every schema of the OpenAPI document, and every field of it, is described.

A schema's description is its model's docstring; a field's is its
``Field(description=...)``.  A field that only refers to another schema
(``$ref``, ``allOf``) is described only by a description of its own: the
referenced schema's says what that type is, not what the field holds.
FastAPI's own validation-error schemas are left out.
"""

from __future__ import annotations

from typing import Any

import pytest

from graphos.app import create_app
from graphos.config import Settings

# Defined by FastAPI, not by graphos.
EXCLUDED = frozenset({"HTTPValidationError", "ValidationError"})


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    app = create_app(Settings(db_password="x", auth_disabled=True))
    found = app.openapi()["components"]["schemas"]
    return {name: schema for name, schema in found.items() if name not in EXCLUDED}


def _described(schema: dict[str, Any]) -> bool:
    return bool(str(schema.get("description") or "").strip())


def test_there_are_schemas(schemas: dict[str, dict[str, Any]]) -> None:
    assert len(schemas) > 100


def test_every_schema_has_a_description(schemas: dict[str, dict[str, Any]]) -> None:
    missing = sorted(name for name, schema in schemas.items() if not _described(schema))
    assert not missing, f"schemas without a description (a docstring): {missing}"


def test_every_field_has_a_description(schemas: dict[str, dict[str, Any]]) -> None:
    missing = sorted(
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field, prop in (schema.get("properties") or {}).items()
        if not _described(prop)
    )
    assert not missing, f"fields without a description (Field(description=...)): {missing}"
