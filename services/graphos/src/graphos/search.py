"""Search indexes: named, per-tenant, ranked search over chosen properties.

The model follows the full-text indexes of other graph databases (Neo4j's
``CREATE FULLTEXT INDEX … FOR (n:A|B) ON EACH [n.p, n.q]`` with an analyzer,
ArangoSearch's analyzers): a tenant declares, at registration, a named index
over some labels (or ``*`` for all) and some properties, with an analyzer that
says how text is broken into searchable pieces.  graphos keeps one Postgres
index per (label, property) behind it and answers queries with scored hits.

Analyzers
---------
``identifier``
    For names, identifiers, codes and paths.  Case-insensitive; splits
    camelCase and acronyms (``parseHTTPResponse`` → ``parse http response``)
    and treats every non-alphanumeric character as a word break, like
    Lucene's word-delimiter filter.  Matching is exact, prefix, contains and
    word-trigram (``pg_trgm`` ``<%``), which already tolerates small typos;
    with ``fuzzy``, when nothing matches literally, whole-value trigram
    similarity (``%``) looks for misspellings too.
``text``
    For prose.  Postgres full-text search with a text-search configuration
    (``language``: ``english``, ``german``, …, or ``simple`` for no
    stemming): stemming, stop words, ``websearch_to_tsquery`` syntax
    (``"exact phrase"``, ``or``, ``-excluded``), ranked by ``ts_rank_cd``.

A query typed without separators (``parsehttpresponse``) matches as it would
with them: exact, prefix and contained matches compare the words run
together, like the catenated token of Lucene's word-delimiter filter.

Scores are in [0, 1].  ``identifier``: 1 for an exact match, [0.9, 1) for a
prefix, [0.8, 0.9) for a contained match, below 0.8 for a trigram match.
Within a tier, a match on whole words and covering more of the value scores
higher.
``text``: ``ts_rank_cd`` normalised to rank / (rank + 1).

Names are trusted: labels, properties and languages are validated before they
reach here (registration contract and :func:`ensure_indexes`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

Analyzer = Literal["identifier", "text"]
ALL_LABELS = "*"
# Candidates read per (table, property, kind of match) before ranking.
CANDIDATES_PER_BRANCH = 200
# Below this many characters a query has no trigram to look up, so only
# exact and prefix matches are tried (both are still served by the index).
MIN_TRIGRAM_QUERY = 3


@dataclass(frozen=True, slots=True)
class SearchIndex:
    name: str
    labels: tuple[str, ...]          # ("*",) for every vertex label
    properties: tuple[str, ...]
    analyzer: Analyzer = "identifier"
    language: str = "simple"         # text analyzer only

    @classmethod
    def from_spec(cls, name: str, body: dict[str, Any]) -> SearchIndex:
        return cls(
            name=name,
            labels=tuple(body.get("labels") or (ALL_LABELS,)),
            properties=tuple(body.get("properties") or ("name",)),
            analyzer=body.get("analyzer", "identifier"),
            language=body.get("language", "simple"),
        )

    def covers(self, label: str) -> bool:
        return ALL_LABELS in self.labels or label in self.labels


@dataclass(frozen=True, slots=True)
class Hit:
    gid: int
    label: str
    score: float
    matched: str          # the property that matched best
    properties: dict[str, Any]


# ── Expressions shared by the indexes and the queries ─────────────────────
#
# The planner uses an expression index only for the identical expression, so
# both sides are built here.  ``alias`` is "" in an index definition and
# "v." in a query.


def value_sql(prop: str, alias: str = "") -> str:
    return f"((({alias}properties)::text)::jsonb ->> '{prop}')"


def identifier_sql(value: str) -> str:
    """Lower-cased words: camelCase and acronyms split, non-alphanumerics → space."""
    split_case = (
        f"regexp_replace(regexp_replace({value}, "
        f"'([[:lower:][:digit:]])([[:upper:]])', '\\1 \\2', 'g'), "
        f"'([[:upper:]])([[:upper:]][[:lower:]])', '\\1 \\2', 'g')"
    )
    return f"btrim(lower(regexp_replace({split_case}, '[^[:alnum:]]+', ' ', 'g')))"


def joined_sql(value: str) -> str:
    """The same words run together (``parsehttpresponse``), as Lucene's
    word-delimiter filter can also emit the catenated token: a query typed
    without separators matches as it would with them."""
    return f"replace({identifier_sql(value)}, ' ', '')"


def text_sql(value: str, language: str) -> str:
    return f"to_tsvector('{language}'::regconfig, coalesce({value}, ''))"


def index_name(label: str, prop: str, kind: str) -> str:
    name = f"{label}_{prop}_{kind}_search"
    if len(name) <= 63:   # noqa: PLR2004 — Postgres identifier limit
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]   # noqa: S324 — a name, not security
    return f"{name[:40]}_{digest}_search"


def index_definitions(prop: str, analyzer: Analyzer, language: str) -> list[tuple[str, str]]:
    """(kind, definition) of each Postgres index behind one property.

    ``identifier``: ``words`` serves similar-word and misspelling matches,
    ``joined`` exact, prefix and contained matches.
    """
    if analyzer == "identifier":
        return [
            ("words", f"USING gin (({identifier_sql(value_sql(prop))}) gin_trgm_ops)"),
            ("joined", f"USING gin (({joined_sql(value_sql(prop))}) gin_trgm_ops)"),
        ]
    return [(f"text_{language}", f"USING gin (({text_sql(value_sql(prop), language)}))")]


async def ensure_indexes(
    conn: asyncpg.Connection,
    tenant: str,
    indexes: dict[str, dict[str, Any]],
    ensure_index,  # noqa: ANN001 — bootstrap._ensure_index_concurrently
) -> None:
    """Build the Postgres indexes behind each search index (concurrently).

    Raises:
        ValueError: an unknown text-search language, or a label that is not
            a vertex label of the tenant.
    """
    existing = {
        r["relname"] for r in await conn.fetch(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE n.nspname = $1 AND p.relname = '_ag_label_vertex';",
            tenant,
        )
    }
    for name, body in indexes.items():
        idx = SearchIndex.from_spec(name, body)
        if idx.analyzer == "text" and not await conn.fetchval(
            "SELECT 1 FROM pg_ts_config WHERE cfgname = $1;", idx.language,
        ):
            raise ValueError(f"search index {name!r}: unknown text-search language "
                             f"{idx.language!r}")
        labels = sorted(existing) if ALL_LABELS in idx.labels else idx.labels
        for label in labels:
            if label not in existing:
                raise ValueError(f"search index {name!r}: {label!r} is not a vertex label")
            for prop in idx.properties:
                for kind, definition in index_definitions(prop, idx.analyzer, idx.language):
                    await ensure_index(
                        conn, tenant, label, index_name(label, prop, kind), definition,
                    )


# ── Queries ────────────────────────────────────────────────────────────────


def _sources(tenant: str, idx: SearchIndex, label: str | None) -> list[str]:
    """Tables to search.  Every vertex label: the AGE parent table, which
    reaches each label's own index through inheritance."""
    if label is not None:
        return [f'"{tenant}"."{label}"']
    if ALL_LABELS in idx.labels:
        return [f'"{tenant}"."_ag_label_vertex"']
    return [f'"{tenant}"."{lab}"' for lab in idx.labels]


def _identifier_branches(tenant: str, idx: SearchIndex, label: str | None, *,
                         fuzzy_pass: bool, short: bool) -> str:
    # One branch per kind of match, each capped, so that a common word cannot
    # crowd out the exact and prefix matches.  The cap takes the first
    # candidates found, not the best: ordering them would read every match
    # (measured: seconds per query on large tenants), which Lucene avoids
    # with skip structures Postgres indexes do not have.
    # Literal matches compare the joined words ({j}), similar words the words
    # themselves ({w}); each condition is served by the index on its side.
    if fuzzy_pass:
        conditions = ["{w} % q.q"]
    else:
        conditions = ["{j} = q.j", "{j} LIKE q.j || '%'"]
        if not short:
            conditions += ["{j} LIKE '%' || q.j || '%'", "q.q <% {w}"]
    branches = []
    for source in _sources(tenant, idx, label):
        for prop in idx.properties:
            words = identifier_sql(value_sql(prop, "v."))
            joined = joined_sql(value_sql(prop, "v."))
            for cond in conditions:
                branches.append(
                    f"(SELECT v.id, v.tableoid, '{prop}' AS prop, {words} AS w, "
                    f"(v.properties::text)::jsonb AS props FROM {source} v, q "
                    f"WHERE {cond.format(w=words, j=joined)} LIMIT {CANDIDATES_PER_BRANCH})"
                )
    return "\nUNION ALL\n".join(branches)


_IDENTIFIER_RANKED = """
WITH q AS (SELECT $1::text AS q, replace($1::text, ' ', '') AS j),
candidates AS ({branches}),
scored AS (
    SELECT c.id, c.tableoid, c.prop, c.w, c.props,
           CASE WHEN c.j = q.j THEN 1.0
                WHEN c.j LIKE q.j || '%' THEN 0.9 + 0.099 * c.closeness
                WHEN c.j LIKE '%' || q.j || '%' THEN 0.8 + 0.099 * c.closeness
                ELSE 0.799 * greatest(word_similarity(q.q, c.w), similarity(c.w, q.q))
           END AS score
    FROM (
        -- Within a tier: whole words matched, and how much of the value the
        -- query covers (shorter values first, like a BM25 length norm).
        SELECT c.*, 0.5 * word_similarity(q.q, c.w)
                    + 0.5 * length(q.j)::float8 / greatest(length(c.j), 1) AS closeness
        FROM (SELECT c.*, replace(c.w, ' ', '') AS j FROM candidates c) c, q
    ) c, q
),
best AS (
    SELECT DISTINCT ON (tableoid, id) id, tableoid, prop, w, props, score
    FROM scored ORDER BY tableoid, id, score DESC, length(w)
)
SELECT (b.id::text)::bigint AS gid, pc.relname AS label, b.prop, b.score::float8 AS score,
       b.w, b.props
FROM best b JOIN pg_class pc ON pc.oid = b.tableoid
WHERE b.score >= $2
ORDER BY b.score DESC, length(b.w), b.id
LIMIT $3;
"""


_TEXT_RANKED = """
WITH q AS (SELECT websearch_to_tsquery('{language}'::regconfig, $1) AS q),
candidates AS ({branches}),
best AS (
    SELECT DISTINCT ON (tableoid, id) id, tableoid, prop, score, props
    FROM candidates ORDER BY tableoid, id, score DESC
)
SELECT (b.id::text)::bigint AS gid, pc.relname AS label, b.prop, b.score::float8 AS score,
       b.props
FROM best b JOIN pg_class pc ON pc.oid = b.tableoid
WHERE b.score >= $2
ORDER BY b.score DESC, b.id
LIMIT $3;
"""


def _text_query(tenant: str, idx: SearchIndex, label: str | None) -> str:
    branches = []
    for source in _sources(tenant, idx, label):
        for prop in idx.properties:
            vector = text_sql(value_sql(prop, "v."), idx.language)
            branches.append(
                f"(SELECT v.id, v.tableoid, '{prop}' AS prop, "
                f"ts_rank_cd({vector}, q.q, 32) AS score, "
                f"(v.properties::text)::jsonb AS props "
                f"FROM {source} v, q WHERE {vector} @@ q.q "
                f"ORDER BY score DESC LIMIT {CANDIDATES_PER_BRANCH})"
            )
    return _TEXT_RANKED.format(language=idx.language, branches="\nUNION ALL\n".join(branches))


def _merge(a: list[asyncpg.Record], b: list[asyncpg.Record], limit: int) -> list[asyncpg.Record]:
    best: dict[tuple[str, int], asyncpg.Record] = {}
    for r in [*a, *b]:
        key = (r["label"], r["gid"])
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: (-r["score"], len(r["w"]), r["gid"]))[:limit]


async def query(  # noqa: PLR0913 — the query's options
    conn: asyncpg.Connection,
    tenant: str,
    idx: SearchIndex,
    q: str,
    *,
    label: str | None,
    limit: int,
    fuzzy: bool,
    min_score: float,
) -> list[Hit]:
    """The best ``limit`` hits for ``q`` in search index ``idx``."""
    if idx.analyzer == "text":
        rows = await conn.fetch(_text_query(tenant, idx, label), q, min_score, limit)
    else:
        words = await conn.fetchval(f"SELECT {identifier_sql('$1::text')};", q)
        if not words:
            return []
        short = len(words.replace(" ", "")) < MIN_TRIGRAM_QUERY
        sql = _IDENTIFIER_RANKED.format(
            branches=_identifier_branches(tenant, idx, label, fuzzy_pass=False, short=short),
        )
        rows = await conn.fetch(sql, words, min_score, limit)
        # Misspellings, on request (fuzzy terms are opt-in in Lucene-based
        # engines too) and only when nothing matched literally: whole-value
        # trigram similarity reads far more of the index than the literal pass.
        literal = any(r["score"] >= 0.8 for r in rows)  # noqa: PLR2004 — contained match or better
        if fuzzy and not literal and not short:
            sql = _IDENTIFIER_RANKED.format(
                branches=_identifier_branches(tenant, idx, label, fuzzy_pass=True, short=False),
            )
            rows = _merge(rows, await conn.fetch(sql, words, min_score, limit), limit)
    return [
        Hit(
            gid=int(r["gid"]), label=str(r["label"]), score=round(float(r["score"]), 4),
            matched=str(r["prop"]), properties=json.loads(r["props"]),
        )
        for r in rows
    ]
