"""The Cypher console: read-only Cypher on one tenant's graph.

On unless ``GRAPHOS_CYPHER=false`` (ADR-0006).  graphos runs the query, so graphos
decides what runs and how (see docs/guide/concept.md §3.1, "Cypher / SQL
boundary"):

- **Only Cypher's own functions.**  AGE lets a Cypher expression call a
  PostgreSQL function by its schema-qualified name (``pg_catalog.x()``), and
  an unqualified name that is not one of AGE's falls through to other
  functions: other extensions' already on AGE 1.6 (``pg_trgm``'s), any on
  the search path from 1.7 (PostgreSQL's built-ins included).  So a query
  is refused before it reaches the database when it calls a function by a
  qualified name (:func:`qualified_calls`) or one that is not Cypher's
  (:func:`unknown_calls`, against :data:`CYPHER_FUNCTIONS`), whatever the
  database user may do and whichever AGE runs.
- **Read-only, one tenant.**  A query with a write clause is refused before
  the database (:func:`write_clauses`): AGE 1.6 carries out ``SET``,
  ``REMOVE`` and ``DELETE`` (``DETACH DELETE`` too) itself, where
  PostgreSQL's read-only check does not see them; it stops only ``CREATE``
  and ``MERGE``.  The query then runs in ``BEGIN READ ONLY`` as well;
  graphos writes the graph name; the user's text goes inside a random dollar
  tag it does not contain; parameters go as one agtype map.  Writes stay on
  the API, which keeps revisions, the change feed and the audit log right.
- **Bounded and stoppable.**  ``LIMIT n + 1`` (to say a result was cut);
  ``statement_timeout``; a watchdog that cancels, then terminates, the
  backend if a query outlives the timeout or the caller goes away (AGE
  sees a cancel late while it builds large lists), and, should the backend
  still not stop, closes the connection so the caller is answered and the
  slot freed; one query per user at a time, on a small pool of its own (a
  user's next query waits its turn, as a query waits for a connection: up
  to ``GRAPHOS_CYPHER_WAIT_S`` in all, then 429).
- **Nothing that needs AGE's in-memory graph** (:func:`needs_age_graph`):
  variable-length patterns (``-[*1..3]->``) and ``vertex_stats()``.  AGE
  serves them from a copy of the whole graph it loads under a process-wide
  lock, and refuses to load label tables with more than its own columns, as
  graphos's are (``graphos_rev``, the community columns).  The failed load
  leaves the lock taken (the error unwinds past the unlock), so the next such
  call in that database process waits on it forever: no cancel, terminate or
  shutdown reaches it.
- **A connection is not reused after a failed query**: a failed AGE call can
  leave its database process unusable, so the console closes it.

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
from typing import Any, cast

import asyncpg

from graphos.config import Settings
from graphos.db import _on_acquire, _on_connect

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 20_000
APPLICATION_NAME = "graphos cypher console"
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


_WRITE_CLAUSES = frozenset({"create", "merge", "set", "delete", "detach", "remove"})


def write_clauses(query: str) -> list[str]:
    """The write clauses in ``query`` (``CREATE``, ``MERGE``, ``SET``, ``DELETE``,
    ``DETACH``, ``REMOVE``), outside strings, comments and quoted names, and not
    as a label (``:Set``), property (``n.set``), map key (``{set: 1}``) or
    parameter (``$set``)."""
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    found = []
    for i, (kind, text) in enumerate(tokens):
        if kind != "word" or text.lower() not in _WRITE_CLAUSES:
            continue
        if i and tokens[i - 1][1] in (".", ":", "$"):
            continue
        if i + 1 < len(tokens) and tokens[i + 1][1] == ":":
            continue
        found.append(text.upper())
    return found


# Cypher's functions as AGE documents them, by the lower-case name it
# resolves (``ag_catalog.age_<name>``), with ``count`` and ``coalesce``, which
# AGE hands to PostgreSQL by design.  AGE's other ``age_*`` functions are its
# internals (``vle``, ``prepare_cypher``, ``delete_global_graphs``, ...).
CYPHER_FUNCTIONS = frozenset({
    # predicates and scalars
    "exists", "isempty", "id", "start_id", "end_id", "type", "label", "properties",
    "head", "last", "length", "size", "startnode", "endnode", "timestamp", "coalesce",
    "toboolean", "tofloat", "tointeger", "tostring",
    # lists
    "keys", "labels", "nodes", "relationships", "range", "reverse", "tail",
    "tobooleanlist", "tofloatlist", "tointegerlist", "tostringlist",
    # numbers
    "rand", "abs", "ceil", "floor", "round", "sign", "e", "sqrt", "exp", "log", "log10",
    "degrees", "radians", "pi", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "cot",
    # strings
    "replace", "split", "left", "right", "substring", "ltrim", "rtrim", "trim",
    "tolower", "toupper",
    # aggregation
    "count", "avg", "sum", "min", "max", "collect", "stdev", "stdevp",
    "percentilecont", "percentiledisc",
})

# Words a "(" follows in Cypher's own syntax, not in a call.
_SYNTAX_BEFORE_PAREN = frozenset({
    "match", "optional", "where", "return", "with", "unwind", "as", "and", "or", "xor",
    "not", "in", "is", "create", "merge", "set", "delete", "detach", "remove", "order",
    "by", "skip", "limit", "distinct", "case", "when", "then", "else", "end", "union",
    "call", "yield", "starts", "ends", "contains", "on", "all", "any", "none", "single",
})

# AGE's Cypher functions that load AGE's in-memory copy of the graph
# (``shortest_path`` and ``all_shortest_paths`` from AGE 1.8).
_GRAPH_LOADING_FUNCTIONS = frozenset({"vertex_stats", "shortest_path", "all_shortest_paths"})


def unknown_calls(query: str) -> list[str]:
    """The functions ``query`` calls without a schema that are not Cypher's
    (:data:`CYPHER_FUNCTIONS`), outside strings and comments, as written."""
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    found = []
    for i, (kind, text) in enumerate(tokens[:-1]):
        if kind not in ("word", "tick") or tokens[i + 1][1] != "(":
            continue
        if i and tokens[i - 1][1] == ".":
            continue  # a qualified call: qualified_calls refuses it
        name = text.strip("`")
        if name.lower() not in CYPHER_FUNCTIONS and name.lower() not in _SYNTAX_BEFORE_PAREN:
            found.append(name)
    return found


def needs_age_graph(query: str) -> str | None:
    """What in ``query`` would make AGE load its in-memory graph (which it
    cannot on graphos's tables, see the module notes), or ``None``."""
    if variable_length(query):
        return "variable-length patterns (-[*1..3]->)"
    tokens = [
        (m.lastgroup or "", m.group())
        for m in _TOKEN.finditer(query)
        if m.lastgroup not in ("ws", "comment")
    ]
    for (kind, text), (_, after) in zip(tokens, tokens[1:], strict=False):
        if kind == "word" and text.lower() in _GRAPH_LOADING_FUNCTIONS and after == "(":
            return f"{text}()"
    return None


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
        graph.nodes[cast(int, value.get("id"))] = node
        return {"node": node}
    if isinstance(value, dict) and value.get("__ag") == "edge":
        edge = {"type": value.get("label"), "properties": value.get("properties") or {},
                "start": value.get("start_id"), "end": value.get("end_id")}
        graph.edges[cast(int, value.get("id"))] = edge
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
    _turns: asyncio.Condition = field(default_factory=asyncio.Condition)

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
        writes = write_clauses(text)
        if writes:
            raise ConsoleError(
                400,
                "the console only reads: write through the API ("
                + ", ".join(dict.fromkeys(writes)) + ")",
            )
        calls = qualified_calls(text)
        if calls:
            raise ConsoleError(
                400,
                "the console runs Cypher's own functions only, not database functions: "
                + ", ".join(sorted(set(calls))),
            )
        needs = needs_age_graph(text)
        unknown = [name for name in unknown_calls(text)
                   if name.lower() not in _GRAPH_LOADING_FUNCTIONS]
        if unknown and not needs:
            raise ConsoleError(
                400,
                "the console runs Cypher's own functions only; not one of them: "
                + ", ".join(sorted(set(unknown))),
            )
        if needs:
            raise ConsoleError(
                400,
                f"the console does not run {needs}: AGE serves them from an in-memory copy of "
                "the graph it cannot build on graphos's tables. Write the hops out "
                "(-[:R]->()-[:R]->(), OPTIONAL MATCH for the further ones), or use graphos's "
                "paths (POST /v1/{tenant}/graph/paths, the Path page)",
            )
        cap = min(limit or self.settings.cypher_max_rows, self.settings.cypher_max_rows)
        tag = f"$q_{secrets.token_hex(6)}$"
        while tag in text:
            tag = f"$q_{secrets.token_hex(6)}$"
        names = return_columns(text)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.cypher_wait_s
        await self._take_turn(consumer, deadline)
        try:
            try:
                conn = await asyncio.wait_for(
                    self.pool.acquire(), timeout=max(0.0, deadline - loop.time()),
                )
            except TimeoutError as exc:
                raise ConsoleError(429, "the console is busy; try again in a moment") from exc
            try:
                return await self._on(conn, tenant, text, tag, names, params, cap, gone)
            except BaseException:
                # A failed AGE call can leave its database process unusable
                # (a lock it took and did not release): never reuse it. When the
                # watchdog closed it already, asyncpg has handed it back to the
                # pool and refuses the call.
                with contextlib.suppress(asyncpg.InterfaceError):
                    conn.terminate()
                raise
            finally:
                await self.pool.release(conn)
        finally:
            await self._end_turn(consumer)

    async def _take_turn(self, consumer: str, deadline: float) -> None:
        """One of ``consumer``'s ``GRAPHOS_CYPHER_PER_USER`` turns, waited for until
        ``deadline``: an agent sends its queries together, a person seldom does."""
        loop = asyncio.get_running_loop()
        async with self._turns:
            while self._running.get(consumer, 0) >= self.settings.cypher_per_user:
                left = deadline - loop.time()
                if left <= 0:
                    waited = f"{self.settings.cypher_wait_s:g}"
                    raise ConsoleError(
                        429, f"your other query did not finish within {waited} s; "
                        "wait for it or cancel it",
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._turns.wait(), left)
            self._running[consumer] = self._running.get(consumer, 0) + 1

    async def _end_turn(self, consumer: str) -> None:
        async with self._turns:
            self._running[consumer] -= 1
            if not self._running[consumer]:
                del self._running[consumer]
            self._turns.notify_all()

    async def _on(  # noqa: PLR0913
        self, conn: asyncpg.Connection, tenant: str, text: str, tag: str,
        names: list[str] | None, params: dict[str, Any] | None, cap: int,
        gone: Callable[[], Awaitable[bool]],
    ) -> dict[str, Any]:
        timeout_ms = int(self.settings.cypher_timeout_s * 1000)
        stopped: dict[str, str] = {}
        done = asyncio.Event()
        watchdog = asyncio.create_task(self._watch(conn, gone, stopped, done))
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
            done.set()
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            except Exception:
                # The watchdog's own trouble is not the query's answer.
                log.exception("cypher console: the watchdog failed")

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
        stopped: dict[str, str], done: asyncio.Event,
    ) -> None:
        """Cancel, then terminate, the backend when the caller goes away or the
        query outlives its timeout (the statement timeout should stop it first).

        Returns as soon as ``done`` is set, when the query has ended.  A cancel
        alone did not always stop it (most likely when it arrives while ``gone``
        runs Starlette's ``is_disconnected``, inside an anyio scope that cancels
        itself): the watchdog then went on to its deadline and stopped an idle
        backend, holding a finished query's answer for the whole timeout."""
        pid = conn.get_server_pid()
        deadline = time.monotonic() + self.settings.cypher_timeout_s + WATCHDOG_GRACE_S
        while not await _set_within(done, 0.5):
            caller_gone = await gone()
            if done.is_set():
                return
            if caller_gone:
                stopped["why"] = "gone"
            elif time.monotonic() > deadline:
                stopped["why"] = "overran"
                log.warning("cypher console: a query outlived its timeout", extra={"pid": pid})
            else:
                continue
            await self.owner.execute("SELECT pg_cancel_backend($1);", pid)
            if await _set_within(done, WATCHDOG_TERMINATE_AFTER_S):
                return
            await self.owner.execute("SELECT pg_terminate_backend($1);", pid)
            if await _set_within(done, WATCHDOG_TERMINATE_AFTER_S):
                return
            # Still here: the backend has not seen either (AGE checks for them
            # only now and then). Close the connection, so the caller is answered
            # and the slot is freed; the backend ends when it next checks. When
            # the terminate did reach it, asyncpg has closed and released the
            # connection already, and refuses the call.
            log.warning(
                "cypher console: a backend did not stop; its connection is closed",
                extra={"pid": pid},
            )
            with contextlib.suppress(asyncpg.InterfaceError):
                conn.terminate()
            return


async def _set_within(event: asyncio.Event, seconds: float) -> bool:
    """Whether ``event`` is set within ``seconds``."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), seconds)
    return event.is_set()


async def start(settings: Settings, owner: asyncpg.Pool) -> Console | None:
    """The console for this process, or ``None`` when it is off: a small pool
    of its own, as graphos's own user, so queries never take the API's
    connections."""
    if not settings.cypher:
        return None
    # Set when each connection opens, so the RESET ALL of a release keeps them.
    server_settings = {
        "statement_timeout": str(int(settings.cypher_timeout_s * 1000)),
        "jit": "off",
        # Its backends, told apart in pg_stat_activity.
        "application_name": APPLICATION_NAME,
    }
    temp_limit = settings.cypher_temp_file_limit
    if temp_limit != "-1":
        if await _may_set(owner, "temp_file_limit", temp_limit):
            server_settings["temp_file_limit"] = temp_limit
        else:
            log.warning(
                "cypher console: temp_file_limit not applied (graphos's role may not set it); "
                "a console query's temporary files are bounded only by the disk. "
                "As a superuser: GRANT SET ON PARAMETER temp_file_limit TO <graphos's role>",
            )
    pool = await asyncpg.create_pool(
        dsn=settings.dsn(),
        min_size=0,
        max_size=settings.cypher_pool_size,
        init=_on_connect,
        setup=_on_acquire,
        server_settings=server_settings,
        command_timeout=None,
    )
    assert pool is not None
    log.info(
        "cypher console on",
        extra={"pool": settings.cypher_pool_size, "temp_file_limit": server_settings.get(
            "temp_file_limit", "unbounded")},
    )
    return Console(pool=pool, owner=owner, settings=settings)


async def _may_set(owner: asyncpg.Pool, name: str, value: str) -> bool:
    """Whether graphos's role may set a superuser-level parameter (and the
    value is one PostgreSQL takes), tried in a transaction that is rolled back."""
    try:
        async with owner.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL {name} = '{value}';")
    except asyncpg.InsufficientPrivilegeError:
        return False
    return True
