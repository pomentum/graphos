"""The change feed over HTTP: pages, the head position, and a live stream.

- ``GET /v1/{tenant}/changes?after=&limit=``: the records after a position.
- ``GET /v1/{tenant}/changes/head``: the position to start a sync from.
- ``GET /v1/{tenant}/changes/stream``: Server-Sent Events, one ``change``
  event per record, its position as the event id, so a client resumes with
  ``Last-Event-ID`` after a reconnect.

Ordering, visibility and retention are :mod:`graphos.changes`'s.  A cursor
whose records were pruned answers 410: the consumer resyncs from the head.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from graphos import changes
from graphos.api.graph import _check_tenant
from graphos.auth import ReadPrincipal
from graphos.contracts.changes import (
    DEFAULT_CHANGES_LIMIT,
    MAX_CHANGES_LIMIT,
    ChangeItem,
    ChangesHead,
    ChangesResponse,
)

router = APIRouter(tags=["changes"], prefix="/{tenant}/changes")

# Records read per query while streaming.
_STREAM_BATCH = 500

_GONE_RESPONSE = {
    410: {"description": "The records after this cursor were pruned; resync from the head."},
}


def _cursor(text: str | None) -> changes.Cursor | None:
    if text is None or text == "":
        return None
    try:
        return changes.Cursor.parse(text)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _gone(exc: changes.CursorGoneError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            f"changes up to {exc.horizon} have been pruned; read the data again and "
            "follow the feed from GET …/changes/head"
        ),
    )


def item(change: changes.Change) -> ChangeItem:
    is_edge = change.kind == "edge"
    return ChangeItem(
        cursor=str(change.cursor),
        at=change.at,
        op=change.op,
        kind=change.kind,
        label=change.label,
        src_label=change.src_label,
        dst_label=change.dst_label,
        ids=None if is_edge else change.ids,
        edges=[tuple(p) for p in change.ids] if is_edge and change.ids is not None else None,
        rev=change.rev,
        count=change.count,
        detail=change.detail,
        consumer=change.consumer,
        trace_id=change.trace_id,
    )


@router.get(
    "",
    response_model=ChangesResponse,
    summary="Changes after a position",
    description=(
        "Records in feed order after `after` (from the oldest kept when absent).  Pass "
        "`next` back as `after` to continue; `more` means the page was full.  Delivery "
        "is at least once and never skips a committed change."
    ),
    responses=_GONE_RESPONSE,
)
async def list_changes(
    tenant: str,
    request: Request,
    principal: ReadPrincipal,
    after: Annotated[str | None, Query(description="A cursor from an earlier page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CHANGES_LIMIT)] = DEFAULT_CHANGES_LIMIT,
) -> ChangesResponse:
    _check_tenant(tenant, principal)
    position = _cursor(after)
    async with request.app.state.pool.acquire() as conn:
        try:
            page = await changes.read(conn, tenant, position, limit)
        except changes.CursorGoneError as exc:
            raise _gone(exc) from exc
    return ChangesResponse(
        tenant=tenant,
        changes=[item(c) for c in page.changes],
        next=str(page.next),
        more=page.more,
    )


@router.get(
    "/head",
    response_model=ChangesHead,
    summary="The position to start following the feed from",
    description=(
        "To sync from scratch: take the head, read the data, then follow the feed from "
        "the head.  Changes the read already saw may come again; none is missed."
    ),
)
async def changes_head(tenant: str, request: Request, principal: ReadPrincipal) -> ChangesHead:
    _check_tenant(tenant, principal)
    async with request.app.state.pool.acquire() as conn:
        cursor = await changes.head(conn)
    return ChangesHead(tenant=tenant, cursor=str(cursor))


@router.get(
    "/stream",
    summary="Follow the feed live (Server-Sent Events)",
    description=(
        "`text/event-stream`: a `change` event per record (`id` = its cursor, `data` = the "
        "record as in `GET …/changes`), a comment line when idle, and a final `gone` event "
        "if the stream falls behind retention.  Starts after `Last-Event-ID`, else after "
        "`after`, else at the oldest record kept."
    ),
    responses={200: {"content": {"text/event-stream": {}}}, **_GONE_RESPONSE},
)
async def stream_changes(
    tenant: str,
    request: Request,
    principal: ReadPrincipal,
    after: Annotated[str | None, Query(description="Start after this cursor.")] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    _check_tenant(tenant, principal)
    position = _cursor(last_event_id or after)
    pool: asyncpg.Pool = request.app.state.pool
    # A position that is already gone fails here, with a status code,
    # rather than as an event after a 200.
    async with pool.acquire() as conn:
        try:
            first = await changes.read(conn, tenant, position, _STREAM_BATCH)
        except changes.CursorGoneError as exc:
            raise _gone(exc) from exc
    return StreamingResponse(
        _events(request, tenant, first),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event(change: changes.Change) -> str:
    data = item(change).model_dump_json()
    return f"id: {change.cursor}\nevent: change\ndata: {data}\n\n"


async def _events(request: Request, tenant: str, page: changes.Page) -> AsyncIterator[str]:
    settings = request.app.state.settings
    pool: asyncpg.Pool = request.app.state.pool
    feed: changes.ChangeFeed | None = getattr(request.app.state, "change_feed", None)
    yield "retry: 2000\n\n"
    quiet_since = time.monotonic()
    while True:
        for change in page.changes:
            yield _event(change)
        if page.changes:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= settings.changes_heartbeat_s:
            yield ": keep-alive\n\n"
            quiet_since = time.monotonic()
        if not page.more:
            if feed is not None:
                await feed.wait(tenant)
            else:
                await asyncio.sleep(settings.changes_poll_s)
        if await request.is_disconnected():
            return
        try:
            async with pool.acquire() as conn:
                page = await changes.read(conn, tenant, page.next, _STREAM_BATCH)
        except changes.CursorGoneError as exc:
            detail = {"status": 410, "detail": _gone(exc).detail}
            yield f"event: gone\ndata: {json.dumps(detail)}\n\n"
            return
