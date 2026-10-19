"""DB tests: search quality held to floors (recall@10 and MRR), per analyzer.

DB-gated.  A labelled query set runs through the real ``graph/search`` route,
so a change to the ranking tiers, the trigram thresholds, ``fuzzy`` or the
``text`` analyzer that makes results worse fails here, not in a user's hands.
Each query names the nodes a person would expect; the corpus has look-alike
distractors (``parseHttpRequest`` beside ``parseHttpResponse``, two user
services, several config functions).

The floors sit below the scores measured when they were set (see FLOORS), so
a small tuning change passes and a real regression does not.  Raise them when
the ranking improves.  The data is generic: identifiers in the usual code
styles and English sentences.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_search_quality"
INDEXES: dict[str, Any] = {
    "names": {"properties": ["name", "qualified_name"]},
    "prose": {"labels": ["Doc"], "properties": ["summary"], "analyzer": "text",
              "language": "english"},
}
FUNCTIONS = [
    "parseHttpResponse", "parseHttpRequest", "parseHeaders", "parseJson", "parse",
    "serializeJson", "getUserById", "getUserByEmail", "get_user_name", "fetchUserProfile",
    "updateUserProfile", "deleteUser", "createOrder", "cancelOrder", "getOrderTotal",
    "calculateTax", "calculateShipping", "sendEmail", "sendSms", "renderTemplate",
    "loadConfig", "saveConfig", "retryWithBackoff", "openDatabaseConnection", "closeConnection",
]
CLASSES = [
    "UserService", "UserServiceImpl", "UserRepository", "OrderService", "OrderRepository",
    "HttpClient", "HttpServer", "XMLHttpRequest", "JsonParser", "ConfigLoader", "EmailSender",
    "TaxCalculator", "RetryPolicy", "DatabasePool",
]
DOCS = {
    "d1": "Retries failed payments with exponential backoff.",
    "d2": "The payment gateway validates cards before charging them.",
    "d3": "Configures logging for the scheduler.",
    "d4": "Sends a welcome email to new users after sign-up.",
    "d5": "Calculates sales tax for each order line.",
    "d6": "Caches user profiles to reduce database load.",
    "d7": "Exports monthly invoices as PDF files.",
    "d8": "Rotates encryption keys every ninety days.",
    "d9": "Schedules nightly backups of the database.",
    "d10": "Validates shipping addresses against the postal service.",
}

# (query, the names a person expects); the first group is literal.
IDENTIFIER = [
    ("parseHttpResponse", {"parseHttpResponse"}),
    ("parse http", {"parseHttpResponse", "parseHttpRequest"}),
    ("user service", {"UserService"}),
    ("UserServiceImpl", {"UserServiceImpl"}),
    ("order repo", {"OrderRepository"}),
    ("getUserById", {"getUserById"}),
    ("user by email", {"getUserByEmail"}),
    ("get user name", {"get_user_name"}),
    ("tax", {"calculateTax", "TaxCalculator"}),
    ("shipping", {"calculateShipping"}),
    ("json parser", {"JsonParser"}),
    ("xml http", {"XMLHttpRequest"}),
    ("config", {"loadConfig", "saveConfig", "ConfigLoader"}),
    ("send email", {"sendEmail", "EmailSender"}),
    ("backoff", {"retryWithBackoff"}),
    ("database pool", {"DatabasePool"}),
]
MISSPELLED = [
    ("UserSerivce", {"UserService"}),
    ("calculte tax", {"calculateTax"}),
    ("renderTempalte", {"renderTemplate"}),
    ("connetion", {"closeConnection", "openDatabaseConnection"}),
    ("shiping", {"calculateShipping"}),
    ("JsonPraser", {"JsonParser"}),
]
TEXT = [
    ("payment retries", {"d1"}),
    ("validate cards", {"d2"}),
    ("email new users", {"d4"}),
    ("tax order", {"d5"}),
    ("database backups", {"d9"}),
    ("user profile cache", {"d6"}),
    ("invoice export", {"d7"}),
    ("encryption keys", {"d8"}),
    ("shipping address", {"d10"}),
    ("logging scheduler", {"d3"}),
]

# (recall@10, MRR) each query set must reach.  Measured when set (2026-10-16, PostgreSQL 18 /
# AGE 1.8): identifier 1.000 / 1.000, misspelled 1.000 / 0.917, text 1.000 / 1.000.
FLOORS = {
    "identifier": (0.95, 0.90),
    "misspelled": (0.80, 0.70),
    "text": (0.95, 0.90),
}


def _app(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


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
        resp = await client.post("/v1/tenants", json={
            "name": TENANT, "vertex_labels": ["Function", "Class", "Doc"],
            "search_indexes": INDEXES,
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        for label, names, package in (
            ("Function", FUNCTIONS, "app.core"), ("Class", CLASSES, "com.example"),
        ):
            await writes.upsert_nodes(db_pool, tenant=TENANT, label=label, rows=[
                {"id": n, "props": {"name": n, "qualified_name": f"{package}.{n}"}}
                for n in names
            ])
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
            {"id": i, "props": {"name": i, "summary": s}} for i, s in DOCS.items()
        ])
        yield client
    await _fresh(db_pool)


async def _scores(
    api: httpx.AsyncClient, cases: list[tuple[str, set[str]]], **params: str,
) -> tuple[float, float, list[str]]:
    """Mean recall@10 and MRR over ``cases``, and the queries that missed."""
    recall = mrr = 0.0
    missed: list[str] = []
    for q, expected in cases:
        resp = await api.get(
            f"/v1/{TENANT}/graph/search", params={"q": q, "limit": "10", **params},
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
        names = [n["properties"]["name"] for n in resp.json()["nodes"]]
        recall += len(expected & set(names)) / len(expected)
        first = next((i for i, n in enumerate(names, 1) if n in expected), None)
        mrr += 1 / first if first else 0.0
        if first != 1:
            missed.append(f"{q!r} -> {names[:3]}")
    return recall / len(cases), mrr / len(cases), missed


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "cases", "params"), [
    ("identifier", IDENTIFIER, {"index": "names"}),
    ("misspelled", MISSPELLED, {"index": "names", "fuzzy": "true"}),
    ("text", TEXT, {"index": "prose"}),
])
async def test_search_quality_stays_above_its_floors(
    api: httpx.AsyncClient, kind: str, cases: list[tuple[str, set[str]]], params: dict[str, str],
) -> None:
    recall, mrr, missed = await _scores(api, cases, **params)
    floor_recall, floor_mrr = FLOORS[kind]
    print(f"{kind}: recall@10 {recall:.3f}, MRR {mrr:.3f}; not first: {missed}")  # noqa: T201
    assert recall >= floor_recall, (kind, recall, missed)
    assert mrr >= floor_mrr, (kind, mrr, missed)
