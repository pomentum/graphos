"""DB tests: named search indexes and ranked search (``graphos.search``).

DB-gated.  The API is driven in-process through httpx's ASGI transport with
the test pool.  The data is generic: identifiers in the usual code styles
(camelCase, acronyms, snake/kebab case, dotted and slashed paths, non-ASCII
letters) and a few English sentences for the ``text`` analyzer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import search, writes
from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings
from graphos.contracts.tenants import TenantSpecBody

from .conftest import requires_db

TENANT = "graphos_test_search_idx"
INDEXES: dict[str, Any] = {
    "names": {"properties": ["name", "qualified_name"]},            # every label
    "classes": {"labels": ["Class"], "properties": ["name"]},
    "prose": {"labels": ["Doc"], "properties": ["summary"], "analyzer": "text",
              "language": "english"},
}
FUNCTIONS = [
    ("f1", "parseHTTPResponse", "net.http.parseHTTPResponse"),
    ("f2", "parse", "text.parse"),
    ("f3", "parseHeaders", "net.http.parseHeaders"),
    ("f4", "getUserById", "acct.repo.getUserById"),
    ("f5", "get_user_name", "acct.repo.get_user_name"),
    ("f6", "fetchUserProfile", "acct.api.fetchUserProfile"),
    ("f7", "größe_berechnen", "geometrie.größe_berechnen"),
    ("f8", "sendEmail", "mail.sendEmail"),
]
CLASSES = [
    ("c1", "UserService", "com.example.users.UserService"),
    ("c2", "UserServiceImpl", "com.example.users.UserServiceImpl"),
    ("c3", "HttpClient", "com.example.net.HttpClient"),
    ("c4", "XMLHttpRequest", "web.XMLHttpRequest"),
]
DOCS = [
    ("d1", "Retries failed payments with exponential backoff."),
    ("d2", "The payment gateway validates cards before charging them."),
    ("d3", "Configures logging for the scheduler."),
]


def _app(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register(api: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await api.post("/v1/tenants", json={"name": TENANT, **body})


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
        await conn.execute(
            "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", TENANT,
        )
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    async with _app(db_pool) as client:
        resp = await _register(
            client, vertex_labels=["Function", "Class", "Doc"], search_indexes=INDEXES,
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
        for label, rows in (("Function", FUNCTIONS), ("Class", CLASSES)):
            await writes.upsert_nodes(db_pool, tenant=TENANT, label=label, rows=[
                {"id": i, "props": {"name": n, "qualified_name": qn}} for i, n, qn in rows
            ])
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
            {"id": i, "props": {"name": i, "summary": s}} for i, s in DOCS
        ])
        yield client
    await _fresh(db_pool)


async def _search(api: httpx.AsyncClient, q: str, *, status: int = HTTPStatus.OK,
                  **params: Any) -> dict[str, Any]:
    params = {"index": "names", **params}
    params = {k: v for k, v in params.items() if v is not None}
    resp = await api.get(f"/v1/{TENANT}/graph/search", params={"q": q, **params})
    assert resp.status_code == status, resp.text
    return resp.json()


def _names(body: dict[str, Any]) -> list[str]:
    return [n["properties"]["name"] for n in body["nodes"]]


# ── without a database ────────────────────────────────────────────────────


def test_defaults_and_index_names() -> None:
    body = TenantSpecBody.model_validate({"name": "t", "search_indexes": {"names": {}}})
    spec = body.search_indexes["names"].model_dump()
    assert spec == {"labels": ["*"], "properties": ["name"], "analyzer": "identifier",
                    "language": "simple"}
    idx = search.SearchIndex.from_spec("names", spec)
    assert idx.covers("Anything") and idx.labels == ("*",)
    assert not search.SearchIndex.from_spec("c", {"labels": ["Class"]}).covers("Function")
    kinds = [k for k, _ in search.index_definitions("name", "identifier", "simple")]
    assert [search.index_name("Class", "name", k) for k in kinds] == [
        "Class_name_words_search", "Class_name_joined_search",
    ]
    [(text_kind, _)] = search.index_definitions("summary", "text", "english")
    assert search.index_name("Doc", "summary", text_kind) == "Doc_summary_text_english_search"
    long_a = search.index_name("L" * 40, "p" * 30, "words")
    long_b = search.index_name("L" * 40, "q" * 30, "words")
    assert len(long_a) <= 63 and len(long_b) <= 63 and long_a != long_b    # noqa: PLR2004


# ── the identifier analyzer ────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "words"), [
    ("parseHTTPResponse", "parse http response"),
    ("XMLHttpRequest", "xml http request"),
    ("IOError", "io error"),
    ("getHTTP2Status", "get http2 status"),          # digits stay with their word
    ("MAX_RETRY_COUNT", "max retry count"),
    ("com.example.UserService", "com example user service"),
    ("src/main/UserRepo.java", "src main user repo java"),
    ("  __init__  ", "init"),
    ("ÄnderungsZähler", "änderungs zähler"),
])
async def test_identifier_words(db_pool: asyncpg.Pool, value: str, words: str) -> None:
    async with db_pool.acquire() as conn:
        v = "$1::text"
        got = await conn.fetchrow(
            f"SELECT {search.identifier_sql(v)} AS w, {search.joined_sql(v)} AS j;", value,
        )
    assert (got["w"], got["j"]) == (words, words.replace(" ", ""))



@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("q", "expected"), [
    ("parse http response", "parseHTTPResponse"),     # acronym inside camelCase
    ("ParseHttpResponse", "parseHTTPResponse"),       # any casing of the same words
    ("get user by id", "getUserById"),
    ("GET_USER_NAME", "get_user_name"),               # snake case, any case
    ("get-user-name", "get_user_name"),               # kebab = snake
    ("xml http request", "XMLHttpRequest"),           # leading acronym
    ("größe berechnen", "größe_berechnen"),           # non-ASCII letters
    ("Größe_Berechnen", "größe_berechnen"),
    ("parseHTTPResponse", "parseHTTPResponse"),       # the name as stored
    ("parsehttpresponse", "parseHTTPResponse"),       # typed without separators
    ("getuserbyid", "getUserById"),                   # short words run together
    ("xmlhttprequest", "XMLHttpRequest"),
])
async def test_identifier_matches_the_words_of_a_name(
    api: httpx.AsyncClient, q: str, expected: str,
) -> None:
    body = await _search(api, q)
    assert body["index"] == "names"
    top = body["nodes"][0]
    assert (top["properties"]["name"], top["score"], top["matched"]) == (expected, 1.0, "name")


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("q", "expected", "tier"), [
    ("parsehttp", "parseHTTPResponse", 0.9),          # a prefix, typed run together
    ("httpresponse", "parseHTTPResponse", 0.8),       # contained, run together
    ("user by", "getUserById", 0.8),
    ("UserServ", "UserService", 0.9),
])
async def test_prefix_and_contained_with_or_without_separators(
    api: httpx.AsyncClient, q: str, expected: str, tier: float,
) -> None:
    top = (await _search(api, q))["nodes"][0]
    assert top["properties"]["name"] == expected
    assert tier <= top["score"] < tier + 0.1


@requires_db
@pytest.mark.asyncio
async def test_exact_then_prefix_then_contained(api: httpx.AsyncClient) -> None:
    body = await _search(api, "parse")
    names = _names(body)
    assert names[0] == "parse" and body["nodes"][0]["score"] == 1.0
    assert set(names[1:3]) == {"parseHeaders", "parseHTTPResponse"}
    assert all(0.9 <= n["score"] < 1.0 for n in body["nodes"][1:3])

    body = await _search(api, "user")
    scores = {n["properties"]["name"]: n["score"] for n in body["nodes"]}
    prefix = [scores["UserService"], scores["UserServiceImpl"]]
    contained = [scores["getUserById"], scores["get_user_name"], scores["fetchUserProfile"]]
    assert min(prefix) >= 0.9 > max(contained) >= min(contained) >= 0.8
    assert scores["UserService"] > scores["UserServiceImpl"]           # closer: shorter
    assert [n["score"] for n in body["nodes"]] == sorted(scores.values(), reverse=True)


@requires_db
@pytest.mark.asyncio
async def test_a_node_scores_by_its_best_property(api: httpx.AsyncClient) -> None:
    # Only the qualified name has these words.
    top = (await _search(api, "com example users"))["nodes"][0]
    assert top["matched"] == "qualified_name"
    assert top["properties"]["name"] in {"UserService", "UserServiceImpl"}
    assert 0.9 <= top["score"] < 1.0                                    # a prefix of it
    # Both properties match "user service"; the exact name wins over the
    # qualified name containing it, and the node is listed once.
    body = await _search(api, "user service")
    assert body["nodes"][0]["properties"]["name"] == "UserService"
    assert body["nodes"][0]["matched"] == "name"
    ids = [n["id"] for n in body["nodes"]]
    assert len(ids) == len(set(ids))


@requires_db
@pytest.mark.asyncio
async def test_misspellings_need_fuzzy_and_rank_below_literal_matches(
    api: httpx.AsyncClient,
) -> None:
    fuzzy = await _search(api, "UserSerivce", fuzzy="true")
    assert _names(fuzzy)[0] == "UserService"
    assert fuzzy["nodes"][0]["score"] < 0.8                              # not a literal match
    # Without fuzzy (the default), only similar words: too few shared for this one.
    assert (await _search(api, "UserSerivce"))["nodes"] == []
    # A dropped letter keeps enough word trigrams to match either way.
    for fuzzy_param in ("true", "false"):
        body = await _search(api, "getUsrById", fuzzy=fuzzy_param)
        assert _names(body)[0] == "getUserById" and body["nodes"][0]["score"] < 0.8
    # A literal match exists: misspelling candidates are not mixed in.
    body = await _search(api, "parse", fuzzy="true")
    assert all(n["score"] >= 0.8 for n in body["nodes"])


@requires_db
@pytest.mark.asyncio
async def test_misspellings_are_not_mixed_into_literal_matches(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # Data has its own typos: each name is the other's misspelling.
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Function", rows=[
        {"id": "typo", "props": {"name": "userSerivce"}},
    ])
    assert "userSerivce" not in _names(await _search(api, "user service", fuzzy="true"))
    assert _names(await _search(api, "user serivce", fuzzy="true")) == ["userSerivce"]
    # With no literal match for either, both are misspellings of the query.
    body = await _search(api, "usre service", fuzzy="true")
    assert {"UserService", "userSerivce"} <= set(_names(body))


@requires_db
@pytest.mark.asyncio
async def test_short_queries_match_exact_and_prefix_only(api: httpx.AsyncClient) -> None:
    body = await _search(api, "ht")
    assert _names(body) == ["HttpClient"]                    # not "xml http request"
    assert (await _search(api, "___"))["nodes"] == []        # no words at all


@requires_db
@pytest.mark.asyncio
async def test_label_filter_min_score_and_limit(api: httpx.AsyncClient) -> None:
    body = await _search(api, "user", label="Class")
    assert {n["label"] for n in body["nodes"]} == {"Class"}
    body = await _search(api, "user", index="classes")
    assert set(_names(body)) == {"UserService", "UserServiceImpl"}
    body = await _search(api, "user", min_score=0.9)
    assert set(_names(body)) == {"UserService", "UserServiceImpl"}
    body = await _search(api, "user", limit=2)
    assert len(body["nodes"]) == 2 and body["truncated"] is True
    assert _names(body) == ["UserService", "UserServiceImpl"]


@requires_db
@pytest.mark.asyncio
async def test_unknown_index_and_uncovered_label(api: httpx.AsyncClient) -> None:
    await _search(api, "user", index="nope", status=HTTPStatus.NOT_FOUND)
    await _search(api, "user", index="classes", label="Function", status=HTTPStatus.BAD_REQUEST)
    await _search(api, "user", label="Missing", status=HTTPStatus.BAD_REQUEST)
    await _search(api, "user", min_score=2, status=HTTPStatus.UNPROCESSABLE_ENTITY)


@requires_db
@pytest.mark.asyncio
async def test_without_an_index_search_is_unchanged(api: httpx.AsyncClient) -> None:
    body = await _search(api, "User", index=None)
    assert body["index"] is None
    assert set(_names(body)) == {"UserService", "UserServiceImpl", "fetchUserProfile",
                                 "getUserById", "get_user_name"}   # ILIKE on name, as before
    assert all(n["score"] is None and n["matched"] is None for n in body["nodes"])


# ── the text analyzer ──────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("q", "expected"), [
    ("payment", {"d1", "d2"}),                  # stemmed: "payments" matches
    ("charge", {"d2"}),                         # "charging"
    ('"payment gateway"', {"d2"}),              # phrase
    ("payment -backoff", {"d2"}),               # exclusion
    ("scheduler or backoff", {"d1", "d3"}),     # either
    ("the", set()),                             # a stop word only
])
async def test_text_analyzer_stems_and_takes_web_search_syntax(
    api: httpx.AsyncClient, q: str, expected: set[str],
) -> None:
    body = await _search(api, q, index="prose")
    assert set(_names(body)) == expected
    assert all(0 < n["score"] < 1 and n["matched"] == "summary" for n in body["nodes"])


# ── registration ───────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"Bad-Name": {}},
    {"x": {"analyzer": "phonetic"}},
    {"x": {"labels": ["*", "Class"]}},
    {"x": {"properties": []}},
    {"x": {"properties": ["name", "name"]}},
    {"x": {"properties": ["na'me"]}},
    {"x": {"analyzer": "text", "language": "Eng lish"}},
])
async def test_invalid_search_indexes_are_rejected(
    api: httpx.AsyncClient, bad: dict[str, Any],
) -> None:
    resp = await _register(api, vertex_labels=["Class"], search_indexes=bad)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("bad", "message"), [
    ({"x": {"analyzer": "text", "language": "klingon"}}, "unknown text-search language"),
    ({"x": {"labels": ["Nowhere"]}}, "is not a vertex label"),
])
async def test_search_indexes_checked_against_the_database(
    api: httpx.AsyncClient, bad: dict[str, Any], message: str,
) -> None:
    resp = await _register(api, vertex_labels=["Class"], search_indexes=bad)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert message in resp.text
    assert set((await api.get(f"/v1/tenants/{TENANT}")).json()["search_indexes"]) == set(INDEXES)


@requires_db
@pytest.mark.asyncio
async def test_registration_merges_indexes_by_name_and_covers_new_labels(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await _register(api, vertex_labels=["Module"], search_indexes={
        "classes": {"labels": ["Class"], "properties": ["qualified_name"]},   # replaced
        "modules": {"labels": ["Module"]},                                    # added
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    info = (await api.get(f"/v1/tenants/{TENANT}")).json()
    assert set(info["search_indexes"]) == {"names", "classes", "prose", "modules"}
    assert info["search_indexes"]["classes"]["properties"] == ["qualified_name"]
    # "names" covers every label, so also Module, registered after it.
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Module", rows=[
        {"id": "m1", "props": {"name": "billingCore"}},
    ])
    assert _names(await _search(api, "billing core"))[0] == "billingCore"
    assert _names(await _search(api, "billing", index="modules")) == ["billingCore"]
    body = await _search(api, "com example users", index="classes")
    assert body["nodes"][0]["matched"] == "qualified_name"
    async with db_pool.acquire() as conn:
        built = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = 'Module';",
            TENANT,
        )}
    assert {"Module_name_words_search", "Module_name_joined_search",
            "Module_qualified_name_words_search", "Module_qualified_name_joined_search"} <= built


# ── the Postgres indexes are what serves the queries ──────────────────────


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("condition", "index"), [
    ("{j} = 'userservice'", "Class_name_joined_search"),
    ("{j} LIKE 'user%'", "Class_name_joined_search"),
    ("{j} LIKE '%service%'", "Class_name_joined_search"),
    ("'user serv' <% {w}", "Class_name_words_search"),
    ("{w} % 'user serivce'", "Class_name_words_search"),
])
async def test_identifier_conditions_use_the_index(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool, condition: str, index: str,
) -> None:
    words = search.identifier_sql(search.value_sql("name", "v."))
    joined = search.joined_sql(search.value_sql("name", "v."))
    plan = await _plan(db_pool, f'SELECT v.id FROM "{TENANT}"."Class" v '
                                f"WHERE {condition.format(w=words, j=joined)}")
    assert index in plan, plan


@requires_db
@pytest.mark.asyncio
async def test_text_condition_and_every_label_use_the_indexes(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    vector = search.text_sql(search.value_sql("summary", "v."), "english")
    plan = await _plan(db_pool, f'SELECT v.id FROM "{TENANT}"."Doc" v '
                                f"WHERE {vector} @@ websearch_to_tsquery('english', 'payment')")
    assert "Doc_summary_text_english_search" in plan, plan
    # Through the parent table, each label's own index is used.
    joined = search.joined_sql(search.value_sql("name", "v."))
    plan = await _plan(db_pool, f'SELECT v.id FROM "{TENANT}"."_ag_label_vertex" v '
                                f"WHERE {joined} LIKE 'user%'")
    for label in ("Function", "Class", "Doc"):
        assert f"{label}_name_joined_search" in plan, plan


async def _plan(pool: asyncpg.Pool, sql: str) -> str:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off;")
        return "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + sql))
