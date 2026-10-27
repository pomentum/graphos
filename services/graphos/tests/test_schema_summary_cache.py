"""The schema summary's cache, without a database: fresh, stale (answered while
a new one is built behind), and dropped by a schema change."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from graphos.api import schema_summary as ss
from graphos.bootstrap import TenantSpec
from graphos.contracts.schema_summary import SchemaSummaryResponse


def _summary(n: int) -> SchemaSummaryResponse:
    return SchemaSummaryResponse(
        tenant="acme", labels=[], labels_total=n, links=[], links_total=0,
        estimated=False, text=f"build {n}", cached=False, elapsed_ms=0,
    )


class _App:
    class state:  # noqa: N801 -- stands in for app.state
        class pool:  # noqa: N801
            @staticmethod
            def acquire() -> contextlib.AbstractAsyncContextManager[object]:
                return contextlib.nullcontext(object())  # type: ignore[return-value]


class _Request:
    app = _App


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"n": 0, "key": "schema-1", "release": None, "fail": False}
    monkeypatch.setattr(ss, "_cache", {})
    monkeypatch.setattr(ss, "_building", {})
    monkeypatch.setattr(ss, "_refreshing", {})
    monkeypatch.setattr(ss, "registered_tenants", lambda: {"acme": TenantSpec(name="acme")})

    async def fingerprint(*_: object) -> str:
        return str(state["key"])

    async def build(*_: object) -> SchemaSummaryResponse:
        if state["release"] is not None:
            await state["release"].wait()
        if state["fail"]:
            raise RuntimeError("database gone")
        state["n"] += 1
        return _summary(state["n"])

    monkeypatch.setattr(ss, "_fingerprint", fingerprint)
    monkeypatch.setattr(ss, "_build", build)
    return state


def _age(seconds: float) -> None:
    """Makes the kept summary ``seconds`` older."""
    at, key, summary = ss._cache["acme"]
    ss._cache["acme"] = (at - seconds, key, summary)


async def _get() -> SchemaSummaryResponse:
    return await ss.build_summary(_Request(), "acme", None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_fresh_summary_is_answered_from_the_cache(builds: dict[str, Any]) -> None:
    first = await _get()
    again = await _get()
    assert (first.cached, first.text) == (False, "build 1")
    assert (again.cached, again.text) == (True, "build 1")
    assert builds["n"] == 1


@pytest.mark.asyncio
async def test_a_stale_summary_is_answered_at_once_and_rebuilt_behind(
    builds: dict[str, Any],
) -> None:
    await _get()
    _age(ss.TTL_S + 1)
    builds["release"] = asyncio.Event()
    stale = await _get()            # does not wait for the rebuild
    assert (stale.cached, stale.text) == (True, "build 1")
    for _ in range(3):
        assert (await _get()).text == "build 1"
    assert len(ss._refreshing) == 1   # one rebuild, however many stale answers
    builds["release"].set()
    await asyncio.gather(*ss._refreshing.values())
    fresh = await _get()
    assert (fresh.cached, fresh.text) == (True, "build 2")


@pytest.mark.asyncio
async def test_a_schema_change_builds_before_answering(builds: dict[str, Any]) -> None:
    await _get()
    builds["key"] = "schema-2"
    changed = await _get()
    assert (changed.cached, changed.text) == (False, "build 2")


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_summary(builds: dict[str, Any]) -> None:
    await _get()
    _age(ss.TTL_S + 1)
    builds["fail"] = True
    assert (await _get()).text == "build 1"
    await asyncio.gather(*ss._refreshing.values())
    assert (await _get()).text == "build 1"   # still answered; the next call tries again
