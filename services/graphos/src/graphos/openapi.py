"""The OpenAPI document, with the error responses every route shares.

Routes declare what is their own (a 409 for an id in several labels, a 413
for a file too large, …).  What comes from graphos as a whole is added here,
once, to each operation it applies to, so no route can forget it:

- **401 / 403**: a route that needs a token (``security`` in its operation)
  answers 401 without a valid one and 403 when its grants do not allow the
  operation (the access level, or the tenant).
- **404** on every tenant route: the tenant is not registered.  A route that
  declares its own 404 keeps it; the tenant case is added to its description.
- **429**: the per-token rate limit (``GRAPHOS_RATE_LIMIT_ENABLED``).
- **504**: a database statement ran past ``GRAPHOS_STATEMENT_TIMEOUT_S``.

Every error response gets a body schema: ``{"detail": ...}`` as
``HTTPException`` answers, or RFC 7807 ``application/problem+json`` for the
timeout.  A 422 is FastAPI's validation error or a route's own refusal.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

TENANT_404 = "The tenant is not registered."


class ErrorResponse(BaseModel):
    """What graphos answers with an error: why, in ``detail``."""

    detail: Any = Field(..., description="Why the request failed: a message, or a structure.")


class RateLimited(ErrorResponse):
    """A 429 of the per-token rate limit."""

    retry_after_s: float = Field(..., description="Seconds until a request is accepted again.")


class Problem(BaseModel):
    """An RFC 7807 problem (``application/problem+json``): a statement
    timeout (504), unique property values taken (409), or a strict property
    schema refusing rows (422)."""

    type: str = Field("about:blank", description="Always `about:blank`.")
    title: str = Field(
        ...,
        description=(
            "A short summary: `Query timed out`, `Unique property values taken` or "
            "`Property schema violations`."
        ),
    )
    status: int = Field(..., description="The HTTP status code: 504, 409 or 422.")
    detail: str = Field(..., description="What went wrong, and what to do about it.")
    total: int | None = Field(
        None, description="A property schema refusal (422): how many violations there are.",
    )
    violations: list[dict[str, Any]] | None = Field(
        None,
        description="A property schema refusal (422): the first violations, each where and why.",
    )


_SHARED = {
    "401": "No token, or a token that is invalid, expired or revoked.",
    "403": "The token's grants do not allow this operation (its access level, or the tenant).",
    "429": (
        "Too many requests for this token (`GRAPHOS_RATE_LIMIT_ENABLED`); `Retry-After` "
        "says when to try again."
    ),
    "504": "A database statement ran past the statement timeout (`GRAPHOS_STATEMENT_TIMEOUT_S`).",
}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _add(op: dict[str, Any], code: str, description: str, schema: str, media: str) -> None:
    responses = op.setdefault("responses", {})
    if code not in responses:
        responses[code] = {"description": description}
    responses[code].setdefault("content", {media: {"schema": _ref(schema)}})


def _validation_or_check(op: dict[str, Any]) -> None:
    """FastAPI's 422 says the request does not match its schema; routes also
    answer 422 for values they check themselves, with a text ``detail``."""
    response = op.get("responses", {}).get("422")
    media = (response or {}).get("content", {}).get("application/json", {})
    if media.get("schema") != _ref("HTTPValidationError"):
        return
    media["schema"] = {"anyOf": [_ref("HTTPValidationError"), _ref("ErrorResponse")]}
    response["description"] = (
        "The request does not match its schema (`detail` lists where), or a value the route "
        "checks itself is refused (`detail` says why)."
    )


def enrich(schema: dict[str, Any]) -> dict[str, Any]:
    """Adds the shared error responses and error body schemas to ``schema``."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in (ErrorResponse, RateLimited, Problem):
        components[model.__name__] = model.model_json_schema(
            ref_template="#/components/schemas/{model}",
        )
    for path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if method not in ("get", "put", "post", "patch", "delete"):
                continue
            secured = bool(op.get("security"))
            if secured:
                _add(op, "401", _SHARED["401"], "ErrorResponse", "application/json")
                _add(op, "403", _SHARED["403"], "ErrorResponse", "application/json")
                _add(op, "429", _SHARED["429"], "RateLimited", "application/json")
                _add(op, "504", _SHARED["504"], "Problem", "application/problem+json")
            if "{tenant}" in path:
                own = op.get("responses", {}).get("404")
                if own and "not registered" not in own.get("description", "").lower():
                    own["description"] = (
                        f"{own['description'].rstrip('.')}; or the tenant is not registered."
                    )
                _add(op, "404", TENANT_404, "ErrorResponse", "application/json")
            _validation_or_check(op)
            for code, response in op.get("responses", {}).items():
                if code[0] in "45" and "content" not in response:
                    response["content"] = {"application/json": {"schema": _ref("ErrorResponse")}}
    return schema


def install(app: FastAPI) -> None:
    """Serves the enriched document at ``/openapi.json`` (built once)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = enrich(get_openapi(
                title=app.title, version=app.version, description=app.description,
                routes=app.routes, tags=app.openapi_tags, servers=app.servers,
            ))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
