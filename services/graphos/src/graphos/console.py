"""The Cypher console: read-only Cypher on one tenant's graph.

Off unless ``GRAPHOS_CYPHER=true``.  graphos runs the query, so graphos
decides what runs and how (see docs/design, "queries, label styles and
exports", §6):

- **Only Cypher's own functions.**  AGE lets a Cypher expression call a
  PostgreSQL function by its schema-qualified name (``pg_catalog.x()``), and
  only so: an unqualified name resolves to AGE's own functions.  A query
  with a qualified call is refused before it reaches the database
  (:func:`qualified_calls`), whatever the database user may do.
- **Read-only, one tenant.**  ``BEGIN READ ONLY``; graphos writes the graph
  name; the user's text goes inside a random dollar tag it does not
  contain; parameters go as one agtype map.  Writes stay on the API, which
  keeps revisions, the change feed and the audit log right.
- **Bounded and stoppable.**  ``LIMIT n + 1`` (to say a result was cut);
  ``statement_timeout``; a watchdog that cancels, then terminates, the
  backend if a query outlives the timeout or the caller goes away (AGE
  sees a cancel late while it builds large lists), and, should the backend
  still not stop, closes the connection so the caller is answered and the
  slot freed; one query per user, on a small pool of its own.
- **No variable-length patterns** (``-[*1..3]->``) unless
  ``GRAPHOS_CYPHER_VARIABLE_LENGTH`` allows them (:func:`variable_length`):
  AGE's search for them does not check for a cancel, so neither the
  statement timeout nor the watchdog stops one until it is done; it can hold
  a database process for many minutes.

AGE needs the result columns declared: they are read from the query's last
top-level ``RETURN`` (:func:`return_columns`); when that count is wrong, AGE
refuses the column list while planning, before anything runs, and the next
counts are tried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from graphos.config import Settings
from graphos.db import _on_acquire, _on_connect

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 20_000
# Column counts tried when the RETURN cannot be read (RETURN *) or was misread.
MAX_COLUMNS = 32
# How long a query may outlive its statement timeout before the watchdog acts,
# and between its cancel and its terminate.
WATCHDOG_GRACE_S = 5.0
WATCHDOG_TERMINATE_AFTER_S = 3.0
class ConsoleError(Exception):
    """A query the console refuses or AGE rejected; ``status`` is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ── Result columns ─────────────────────────────────────────────────────────

_TOKEN = re.compile(
    r"""(?P<ws>\s+)|(?P<comment>//[^\n]*|/\*.*?\*/)|(?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
    r"""|(?P<tick>`[^`]*`)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<open>[(\[{])|(?P<close>[)\]}])"""
    r"""|(?P<comma>,)|(?P<other>.)""",
    re.S,
)
_RETURN_ENDS = {"ORDER", "SKIP", "LIMIT", "UNION"}


def return_columns(query: str) -> list[str] | None:
    """The names of the last top-level ``RETURN``'s items (its ``AS`` alias, else
    the expression), or ``None`` when there is none to read (``RETURN *``)."""
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    depth, last = 0, None
    for i, (kind, text) in enumerate(tokens):
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
        elif kind == "word" and depth == 0 and text.upper() == "RETURN":
            last = i
    if last is None:
        return None
    rest = tokens[last + 1:]
    if rest and rest[0][0] == "word" and rest[0][1].upper() == "DISTINCT":
        rest = rest[1:]
    items: list[list[tuple[str, str]]] = [[]]
    depth = 0
    for kind, text in rest:
        if depth == 0 and kind == "word" and text.upper() in _RETURN_ENDS:
            break
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
        if depth == 0 and kind == "comma":
            items.append([])
            continue
        items[-1].append((kind, text))
    if len(items) == 1 and items[0] in ([("other", "*")], []):
        return None
    names = []
    for n, item in enumerate(items, 1):
        if len(item) >= 2 and item[-2][0] == "word" and item[-2][1].upper() == "AS":  # noqa: PLR2004
            names.append(item[-1][1].strip("`"))
        else:
            names.append("".join(text for _, text in item) or f"c{n}")
    return names


def qualified_calls(query: str) -> list[str]:
    """The schema-qualified function calls in ``query`` (``pg_catalog.version()``),
    outside strings and comments, however spaced or quoted: only these can
    reach PostgreSQL functions from Cypher, and the console refuses them."""
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    found = []
    for i in range(len(tokens) - 3):
        (k1, t1), (k2, t2), (k3, t3), (k4, t4) = tokens[i:i + 4]
        if k1 in ("word", "tick") and t2 == "." and k3 in ("word", "tick") and t4 == "(":
            found.append(f"{t1.strip('`')}.{t3.strip('`')}")
    return found


def variable_length(query: str) -> bool:
    """Whether ``query`` has a variable-length relationship pattern
    (``-[:R*1..3]->``, ``<-[*]-``): a ``*`` inside brackets that follow a
    dash, outside strings and comments."""
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    depth = 0          # bracket depth inside a relationship pattern, 0 outside one
    for i, (kind, text) in enumerate(tokens):
        if depth:
            if kind == "open":
                depth += 1
            elif kind == "close":
                depth -= 1
            elif text == "*":
                return True
        elif text == "[" and i > 0 and tokens[i - 1][1] == "-":
            depth = 1
    return False


def _column_counts(names: list[str] | None) -> list[int]:
    """The counts to try: the one read first, then the others in order."""
    first = [len(names)] if names else []
    return first + [n for n in range(1, MAX_COLUMNS + 1) if n not in first]


# ── agtype values ──────────────────────────────────────────────────────────

_ANNOTATIONS = ("vertex", "edge", "path", "numeric", "float", "integer")


def agtype_value(raw: str | None) -> Any:
    """An agtype result as Python: ``::vertex`` / ``::edge`` objects marked
    (``"__ag": "vertex"``), other annotations (``::path``, ``::numeric``)
    dropped; strings are never touched."""
    if raw is None:
        return None
    out: list[str] = []
    opens: list[int] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            j = i + 1
            while j < n and raw[j] != '"':
                j += 2 if raw[j] == "\\" else 1
            out.append(raw[i:j + 1])
            i = j + 1
            continue
        if ch == "{":
            opens.append(len(out))
            out.append(ch)
        elif ch == "}":
            start = opens.pop() if opens else None
            out.append(ch)
            for kind in ("vertex", "edge"):
                if raw.startswith("::" + kind, i + 1):
                    if start is not None:
                        out[start] = '{"__ag": "%s", ' % kind  # noqa: UP031
                    i += len(kind) + 2
                    break
        elif ch == ":" and raw.startswith("::", i):
            word = re.match(r"::(\w+)", raw[i:])
            if word and word.group(1) in _ANNOTATIONS:
                i += len(word.group(0))
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    text = "".join(out).strip()
    if text == "null":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass
class _Graph:
    """The vertices and edges a result holds, by graphid."""

    nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    edges: dict[int, dict[str, Any]] = field(default_factory=dict)


def _cell(value: Any, graph: _Graph) -> Any:
    """A value as answered: vertices and edges as ``{"node": …}`` / ``{"edge": …}``."""
    if isinstance(value, dict) and value.get("__ag") == "vertex":
        props = value.get("properties") or {}
        node = {"id": str(props.get("id", value.get("id"))), "label": value.get("label"),
                "properties": props}
        graph.nodes[value.get("id")] = node
        return {"node": node}
    if isinstance(value, dict) and value.get("__ag") == "edge":
        edge = {"type": value.get("label"), "properties": value.get("properties") or {},
                "start": value.get("start_id"), "end": value.get("end_id")}
        graph.edges[value.get("id")] = edge
        return {"edge": edge}
    if isinstance(value, list):
        return [_cell(v, graph) for v in value]
    if isinstance(value, dict):
        return {k: _cell(v, graph) for k, v in value.items()}
    return value


def _resolve_edges(graph: _Graph) -> list[dict[str, Any]]:
    """Edges whose two ends are in the result, named by the ends' graphos ids;
    each edge cell gets its ends too (``None`` when an end is not returned)."""
    out = []
    for edge in graph.edges.values():
        src = graph.nodes.get(edge.pop("start"))
        dst = graph.nodes.get(edge.pop("end"))
        edge.update(
            src_id=src["id"] if src else None, src_label=src["label"] if src else None,
            dst_id=dst["id"] if dst else None, dst_label=dst["label"] if dst else None,
        )
        if src and dst:
            out.append(dict(edge))
    return out


# ── The console ────────────────────────────────────────────────────────────


@dataclass
class Console:
    """The console's pool, roles and limits for this process."""

    pool: asyncpg.Pool
    owner: asyncpg.Pool
    settings: Settings
    _running: dict[str, int] = field(default_factory=dict)

    async def run(  # noqa: PLR0913 — one query's inputs
        self,
        tenant: str,
        query: str,
        *,
        params: dict[str, Any] | None,
        limit: int | None,
        consumer: str,
        gone: Callable[[], Awaitable[bool]],
    ) -> dict[str, Any]:
        """Run ``query`` on ``tenant``'s graph; the answer's shape is ``CypherResult``.

        ``gone`` says whether the caller went away (the query is then stopped).
        """
        text = query.strip()
        if not text:
            raise ConsoleError(400, "the query is empty")
        if len(text) > MAX_QUERY_CHARS:
            raise ConsoleError(400, f"the query is longer than {MAX_QUERY_CHARS} characters")
        calls = qualified_calls(text)
        if calls:
            raise ConsoleError(
                400,
                "the console runs Cypher's own functions only, not database functions: "
                + ", ".join(sorted(set(calls))),
            )
        if not self.settings.cypher_variable_length and variable_length(text):
            raise ConsoleError(
                400,
                "the console does not run variable-length patterns (-[*1..3]->): AGE cannot "
                "stop one once it runs. Write the hops out (-[:R]->()-[:R]->()), or use "
                "graphos's paths (POST /v1/{tenant}/graph/paths, the Path page)",
            )
        cap = min(limit or self.settings.cypher_max_rows, self.settings.cypher_max_rows)
        if self._running.get(consumer, 0) >= self.settings.cypher_per_user:
            raise ConsoleError(429, "you are running a query already; wait for it or cancel it")
        tag = f"$q_{secrets.token_hex(6)}$"
        while tag in text:
            tag = f"$q_{secrets.token_hex(6)}$"
        names = return_columns(text)

        self._running[consumer] = self._running.get(consumer, 0) + 1
        try:
            try:
                conn = await asyncio.wait_for(self.pool.acquire(), timeout=1.0)
            except TimeoutError as exc:
                raise ConsoleError(429, "the console is busy; try again in a moment") from exc
            try:
                return await self._on(conn, tenant, text, tag, names, params, cap, gone)
            finally:
                await self.pool.release(conn)
        finally:
            self._running[consumer] -= 1
            if not self._running[consumer]:
                del self._running[consumer]

    async def _on(  # noqa: PLR0913
        self, conn: asyncpg.Connection, tenant: str, text: str, tag: str,
        names: list[str] | None, params: dict[str, Any] | None, cap: int,
        gone: Callable[[], Awaitable[bool]],
    ) -> dict[str, Any]:
        timeout_ms = int(self.settings.cypher_timeout_s * 1000)
        stopped: dict[str, str] = {}
        watchdog = asyncio.create_task(self._watch(conn, gone, stopped))
        started = time.monotonic()
        rows: list[asyncpg.Record] = []
        count = 0
        try:
            for count in _column_counts(names):
                cols = ", ".join(f"c{i} agtype" for i in range(count))
                sql = (
                    f"SELECT * FROM ag_catalog.cypher('{tenant}', {tag} {text} {tag}, $1::agtype) "
                    f"AS ({cols}) LIMIT {cap + 1}"
                )
                try:
                    async with conn.transaction(readonly=True):
                        await conn.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms';")
                        rows = await conn.fetch(sql, json.dumps(params or {}))
                    break
                except asyncpg.DatatypeMismatchError as exc:
                    if "column definition list" not in str(exc):
                        raise
            else:
                raise ConsoleError(
                    400, f"could not tell the result's columns (tried 1 to {MAX_COLUMNS})",
                )
        except asyncpg.QueryCanceledError as exc:
            if stopped.get("why") == "gone":
                raise ConsoleError(499, "the query was cancelled") from exc
            limit_s = f"{self.settings.cypher_timeout_s:g}"
            raise ConsoleError(
                504, f"the query ran longer than {limit_s} s and was stopped",
            ) from exc
        except (asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError) as exc:
            # The watchdog terminated the backend: AGE took too long to see the cancel.
            if stopped.get("why") == "gone":
                raise ConsoleError(499, "the query was cancelled") from exc
            raise ConsoleError(504, "the query was stopped") from exc
        except asyncpg.ReadOnlySQLTransactionError as exc:
            raise ConsoleError(400, "the console only reads: write through the API") from exc
        except asyncpg.PostgresError as exc:
            raise ConsoleError(400, str(exc)) from exc
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

        graph = _Graph()
        truncated = len(rows) > cap
        cells = [[_cell(agtype_value(row[i]), graph) for i in range(count)] for row in rows[:cap]]
        columns = names if names and len(names) == count else [f"c{i + 1}" for i in range(count)]
        return {
            "columns": columns,
            "rows": cells,
            "graph": {"nodes": list(graph.nodes.values()), "edges": _resolve_edges(graph)},
            "truncated": truncated,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    async def _watch(
        self, conn: asyncpg.Connection, gone: Callable[[], Awaitable[bool]],
        stopped: dict[str, str],
    ) -> None:
        """Cancel, then terminate, the backend when the caller goes away or the
        query outlives its timeout (the statement timeout should stop it first)."""
        pid = conn.get_server_pid()
        deadline = time.monotonic() + self.settings.cypher_timeout_s + WATCHDOG_GRACE_S
        while True:
            await asyncio.sleep(0.5)
            if await gone():
                stopped["why"] = "gone"
            elif time.monotonic() > deadline:
                stopped["why"] = "overran"
                log.warning("cypher console: a query outlived its timeout", extra={"pid": pid})
            else:
                continue
            await self.owner.execute("SELECT pg_cancel_backend($1);", pid)
            await asyncio.sleep(WATCHDOG_TERMINATE_AFTER_S)
            await self.owner.execute("SELECT pg_terminate_backend($1);", pid)
            await asyncio.sleep(WATCHDOG_TERMINATE_AFTER_S)
            # Still here: the backend has not seen either (AGE checks for them
            # only now and then). Close the connection, so the caller is answered
            # and the slot is freed; the backend ends when it next checks.
            log.warning(
                "cypher console: a backend did not stop; its connection is closed",
                extra={"pid": pid},
            )
            conn.terminate()
            return


async def start(settings: Settings, owner: asyncpg.Pool) -> Console | None:
    """The console for this process, or ``None`` when it is off: a small pool
    of its own, as graphos's own user, so queries never take the API's
    connections."""
    if not settings.cypher:
        return None
    pool = await asyncpg.create_pool(
        dsn=settings.dsn(),
        min_size=0,
        max_size=settings.cypher_pool_size,
        init=_on_connect,
        setup=_on_acquire,
        server_settings={
            "statement_timeout": str(int(settings.cypher_timeout_s * 1000)),
            "jit": "off",
        },
        command_timeout=None,
    )
    assert pool is not None
    log.info("cypher console on", extra={"pool": settings.cypher_pool_size})
    return Console(pool=pool, owner=owner, settings=settings)
