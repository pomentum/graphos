"""Validation for the DDL a tenant registration may carry.

``POST /v1/tenants`` accepts two lists of raw statements that graphos runs
during bootstrap: ``indexes`` and ``generated_columns``.  They are admin-only,
but they still must not be a way to run arbitrary SQL.  Each statement must be
exactly one of:

* ``CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <tenant>.<table> ...``
* ``ALTER TABLE <tenant>.<table> ADD COLUMN [IF NOT EXISTS] <col> <type>
  GENERATED ALWAYS AS (<expr>) STORED``

where ``<tenant>`` is the tenant being registered and ``<table>`` is one of its
own labels (or AGE's ``_ag_label_vertex`` / ``_ag_label_edge`` parents).

The check runs on the statement's top-level *skeleton*: string literals and
everything inside parentheses are collapsed, so an expression can be anything
Postgres accepts (it must be immutable for an index or generated column, which
Postgres enforces), while the statement around it cannot smuggle in a second
command.  ``;``, SQL comments and dollar-quoting are rejected anywhere, which
rules out multiple statements.

The skeleton only works if it agrees with Postgres about where every literal
and quoted identifier ends, so anything whose lexing depends on more than the
plain rules is rejected: backslashes, prefixed string literals (``E'..'``,
``U&'..'``, ``B'..'``, ``X'..'``, ``N'..'``), ``U&"..."`` identifiers, and any
non-ASCII character.  What is left is lexed exactly: ``'...'`` with ``''``
escapes (``standard_conforming_strings`` is on by default) and ``"..."`` with
``""`` escapes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_IDENT = r'(?:"[A-Za-z_][A-Za-z0-9_]{0,62}"|[A-Za-z_][A-Za-z0-9_]{0,62})'

_INDEX_SKELETON = re.compile(
    rf"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS {_IDENT} "
    rf"ON (?:ONLY )?(?P<schema>{_IDENT}) ?\. ?(?P<table>{_IDENT}) ?"
    r"(?:USING (?:btree|gin|gist|hash|brin|spgist) ?)?\(\)"
    r"(?: INCLUDE ?\(\))?"
    r"(?: WHERE .+)?$",
    re.IGNORECASE,
)

# Column types a generated column may have.  Only a type name may sit between
# the column name and GENERATED: an open-ended "word word" slot would also
# admit column constraints such as CHECK (...), whose expressions Postgres does
# NOT require to be immutable and which run on every later write.
_COLUMN_TYPES = (
    "text|varchar|character varying|char|character|citext|"
    "smallint|integer|int|int2|int4|int8|bigint|numeric|decimal|real|"
    "double precision|float|float4|float8|boolean|bool|date|"
    "timestamp|timestamptz|time|timetz|interval|uuid|json|jsonb|bytea"
)

_GENERATED_SKELETON = re.compile(
    rf"^ALTER TABLE (?:ONLY )?(?P<schema>{_IDENT}) ?\. ?(?P<table>{_IDENT}) "
    rf"ADD (?:COLUMN )?(?:IF NOT EXISTS )?(?P<column>{_IDENT}) "
    rf"(?:{_COLUMN_TYPES})(?: ?\(\))?(?: ?\[\])? "
    r"GENERATED ALWAYS AS ?\(\) STORED$",
    re.IGNORECASE,
)

_FORBIDDEN = (";", "--", "/*", "*/", "$", "\\")

# An OPENING quote preceded by a letter, digit, underscore or ``&`` starts a
# prefixed literal (E'..', U&'..', B'..', X'..', N'..') or a U&"..." identifier,
# whose escaping rules differ from the plain ones the skeleton implements.
_PREFIX_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_&")
_UNICODE_IDENT = re.compile(r"[Uu]&")

AGE_PARENT_TABLES = frozenset({"_ag_label_vertex", "_ag_label_edge"})

# Columns graphos adds to label tables itself; a generated column may not
# take these names (or any other ``graphos_`` name).
_RESERVED_COLUMNS = frozenset(
    {"id", "properties", "start_id", "end_id", "end_community_id", "start_community_id"},
)
_RESERVED_COLUMN_PREFIX = "graphos_"


def _skeleton(stmt: str) -> str:
    """Collapse string literals and parenthesised groups to ``''`` / ``()``."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(stmt):
        ch = stmt[i]
        if ch in "'\"" and i > 0 and stmt[i - 1] in _PREFIX_CHARS:
            raise ValueError("prefixed string literals and U& identifiers are not allowed")
        if ch == '"':
            # Quoted identifier, honouring the "" escape.  Kept verbatim at
            # the top level (schema/table names); dropped inside parentheses.
            j = i + 1
            while True:
                if j >= len(stmt):
                    raise ValueError("unterminated quoted identifier")
                if stmt[j] == '"':
                    if j + 1 < len(stmt) and stmt[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if depth == 0:
                out.append(stmt[i : j + 1])
            i = j + 1
            continue
        if ch == "'":
            # Skip a string literal, honouring the '' escape.
            j = i + 1
            while True:
                if j >= len(stmt):
                    raise ValueError("unterminated string literal")
                if stmt[j] == "'":
                    if j + 1 < len(stmt) and stmt[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if depth == 0:
                out.append("''")
            i = j + 1
            continue
        if ch == "(":
            if depth == 0:
                out.append("(")
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
            if depth == 0:
                out.append(")")
        elif depth == 0:
            out.append(ch)
        i += 1
    if depth != 0:
        raise ValueError("unbalanced parentheses")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _unquote(ident: str) -> str:
    """Resolve an identifier the way Postgres does.

    Quoted identifiers are exact (``""`` is a literal quote); unquoted ones are
    folded to lower case, so ``Acme`` names schema ``acme``, which may be a
    different tenant than ``"Acme"``.
    """
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.lower()


def validate_ddl(
    stmt: str,
    *,
    kind: str,
    tenant: str,
    tables: Iterable[str],
) -> str:
    """Return ``stmt`` (trailing ``;`` removed) if it is an allowed statement.

    ``kind`` is ``"index"`` or ``"generated_column"``.  Raises ``ValueError``
    with a reason otherwise.
    """
    body = stmt.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if not body.isascii():
        raise ValueError(f"{kind} DDL must be ASCII: {stmt!r}")
    for token in _FORBIDDEN:
        if token in body:
            raise ValueError(f"{kind} DDL must be a single statement without {token!r}: {stmt!r}")
    if _UNICODE_IDENT.search(body):
        raise ValueError(
            f"{kind} DDL may only use plain '...' literals and \"...\" identifiers: {stmt!r}"
        )
    pattern = _INDEX_SKELETON if kind == "index" else _GENERATED_SKELETON
    m = pattern.fullmatch(_skeleton(body))
    if m is None:
        expected = (
            "CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <tenant>.<label> (...)"
            if kind == "index"
            else "ALTER TABLE <tenant>.<label> ADD COLUMN <col> <type> "
            "GENERATED ALWAYS AS (...) STORED"
        )
        raise ValueError(f"{kind} DDL must have the form {expected}: {stmt!r}")
    schema, table = _unquote(m.group("schema")), _unquote(m.group("table"))
    if schema != tenant:
        raise ValueError(f"{kind} DDL must target schema {tenant!r}, not {schema!r}: {stmt!r}")
    allowed = set(tables) | AGE_PARENT_TABLES
    if table not in allowed:
        raise ValueError(
            f"{kind} DDL targets {table!r}, which is not a label of tenant {tenant!r}: {stmt!r}"
        )
    if kind != "index":
        column = _unquote(m.group("column")).lower()
        if column in _RESERVED_COLUMNS or column.startswith(_RESERVED_COLUMN_PREFIX):
            raise ValueError(f"column name {column!r} is reserved by graphos: {stmt!r}")
    return body
