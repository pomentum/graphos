"""Cypher execution helper, for reads.

Graph writes never go through Cypher; they live in :mod:`graphos.writes`.
Responsibilities:

- Take a Cypher template + a tenant name + a Python dict of parameters.
- Wrap them in AGE's ``cypher('<graph>', $$ … $$, <params>::agtype)`` form.
- Pass parameters as a single JSONB-cast-to-agtype value (AGE's only
  parameter-binding mechanism).
- Return rows as ordinary Python dicts. AGE returns each column as
  ``agtype`` text; this module converts back to ints, strings, dicts, lists.

agtype value shapes (observed against AGE 1.6.0):

    int:        "12345"
    bigint id:  "844424930131969"
    string:     '"foo"'
    bool:       "true" / "false"
    null:       "null"
    list:       "[1, 2, 3]"
    map:        '{"name": "foo"}'
    annotated:  '12345::numeric'  or  '{...}::vertex'  or  '{...}::edge'

We strip the optional ``::type`` annotation and JSON-decode the rest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


# Strip the trailing "::vertex" / "::edge" / "::numeric" annotation that AGE
# appends to typed values.
_AGTYPE_ANNOTATION = re.compile(r"::(\w+)$")


def parse_agtype(raw: Any) -> Any:
    """Convert an agtype scalar (string) into a native Python value.

    Returns the input untouched for non-string types — asyncpg has already
    decoded them. Returns ``None`` for the literal ``"null"`` string.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    stripped = _AGTYPE_ANNOTATION.sub("", raw).strip()
    if stripped == "null":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Bare identifier (rare) — return verbatim.
        return stripped


class CypherError(RuntimeError):
    """Wraps an asyncpg error from a Cypher query with the query text for triage."""

    def __init__(self, message: str, *, sqlstate: str | None = None, query: str = ""):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.query = query


async def query_cypher(
    pool: asyncpg.Pool,
    *,
    graph: str,
    cypher: str,
    return_cols: Sequence[tuple[str, str]],
    params: Mapping[str, Any] | None = None,
    timeout_s: float | None = None,
) -> list[dict[str, Any]]:
    """Run a Cypher query against ``graph`` and return rows as Python dicts.

    Args:
        pool:        the asyncpg pool from app.state.
        graph:       AGE graph name (a registered tenant).
        cypher:      Cypher query body. Reference parameters as ``$name``.
        return_cols: list of ``(column_name, agtype_alias)`` pairs. The alias
                     is currently always ``agtype`` — kept as a parameter so
                     future return-type tightening (e.g. ``bigint``) is a
                     one-line change.
        params:      parameters passed to Cypher as ``$<name>``. Bound as a
                     single ``agtype`` map.
        timeout_s:   optional per-statement timeout.

    Returns:
        List of dicts keyed by column name. Each value is the parsed
        Python equivalent of the agtype output.

    Raises:
        CypherError: wraps any postgres error so callers don't have to
                     import asyncpg.
    """
    cols_decl = ", ".join(f"{name} {alias}" for name, alias in return_cols)
    sql = (
        f"SELECT * FROM cypher('{graph}', $$ {cypher} $$, $1::agtype) "
        f"AS ({cols_decl})"
    )
    params_json = json.dumps(params or {})

    async with pool.acquire() as conn:
        if timeout_s is not None:
            await conn.execute(
                f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"
            )
        try:
            rows = await conn.fetch(sql, params_json)
        except asyncpg.exceptions.QueryCanceledError:
            raise   # statement timeout → 504, not a 400
        except asyncpg.exceptions.PostgresError as exc:
            log.warning(
                "cypher query failed",
                extra={"graph": graph, "sqlstate": exc.sqlstate, "error": str(exc)},
            )
            raise CypherError(
                str(exc),
                sqlstate=exc.sqlstate,
                query=cypher.strip(),
            ) from exc

    out: list[dict[str, Any]] = []
    col_names = [name for name, _ in return_cols]
    for row in rows:
        out.append({name: parse_agtype(row[name]) for name in col_names})
    return out
