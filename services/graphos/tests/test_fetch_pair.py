"""A two-way expansion's pair of queries (``graphos.api.graph._fetch_pair``):
the second connection goes back only once its query ended, whatever the first
did, so a failed hop answers its own error, not "another operation is in
progress"."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from graphos.api.graph import _fetch_pair


class _Conn:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail, self.delay, self.busy = fail, delay, False

    async def fetch(self, *_args: object) -> list[str]:
        self.busy = True
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise ValueError("column e.end_id does not exist")
            return ["row"]
        finally:
            self.busy = False


class _Pool:
    def __init__(self) -> None:
        self.released_busy: list[bool] = []

    async def release(self, conn: Any) -> None:
        self.released_busy.append(conn.busy)


async def test_the_second_connection_goes_back_only_once_its_query_ended() -> None:
    pool, first, second = _Pool(), _Conn(fail=True), _Conn(delay=0.05)
    with pytest.raises(ValueError, match="end_id"):
        await _fetch_pair(pool, first, second, ("out", "in"), ())  # type: ignore[arg-type]
    assert pool.released_busy == [False]


async def test_both_answers_come_back_in_order() -> None:
    pool = _Pool()
    out_rows, in_rows = await _fetch_pair(
        pool, _Conn(delay=0.02), _Conn(), ("out", "in"), (),  # type: ignore[arg-type]
    )
    assert (out_rows, in_rows) == (["row"], ["row"])
    assert pool.released_busy == [False]


async def test_a_cancelled_pair_still_waits_for_both_before_giving_one_back() -> None:
    pool = _Pool()
    task = asyncio.ensure_future(
        _fetch_pair(pool, _Conn(delay=1), _Conn(delay=1), ("out", "in"), ()),  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.released_busy == [False]
