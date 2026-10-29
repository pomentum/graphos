"""One client per tenant, each registered once and kept registered.

A service that writes to several tenants (one per customer, project, team…)
needs the same three things every time: a client per tenant sharing one base
URL and credentials, the tenant's schema registered before the first write, and a
way to recover when the server no longer knows a label (the tenant was
re-registered elsewhere with fewer labels, or unregistered). ``GraphosPool``
does all three.

    schema = TenantSchema(vertex_labels=["User", "Document"], edge_labels=["AUTHORED"])
    async with GraphosPool(base_url=URL, auth=PasswordAuth(USER, SECRET), schema=schema) as pool:
        cli = await pool.client("acme")            # registered on first use
        await pool.call("acme", lambda c: c.upsert_nodes(nodes))   # self-heals once
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, TypeVar

import httpx

from graphos_client._auth import from_environment
from graphos_client._client import GraphosClient, GraphosError

T = TypeVar("T")


@dataclass(frozen=True)
class TenantSchema:
    """What to register for every tenant the pool serves.

    ``edge_labels`` entries are plain names or ``{"name": ..., "resolve_rule": {...}}``
    dicts; ``property_indexes`` / ``generated_columns`` are declarations, as for
    :meth:`GraphosClient.register_tenant` (no SQL, and nothing tenant-specific
    in them).
    """

    vertex_labels: tuple[str, ...] = ()
    edge_labels: tuple[str | Mapping[str, Any], ...] = ()
    property_indexes: tuple[Mapping[str, Any], ...] = ()
    generated_columns: tuple[Mapping[str, Any], ...] = ()
    label_hierarchy: Mapping[str, str] = field(default_factory=dict)
    meta: Mapping[str, str] = field(default_factory=dict)

    def __init__(
        self,
        *,
        vertex_labels: Iterable[str] = (),
        edge_labels: Iterable[str | Mapping[str, Any]] = (),
        property_indexes: Iterable[Mapping[str, Any]] = (),
        generated_columns: Iterable[Mapping[str, Any]] = (),
        label_hierarchy: Mapping[str, str] | None = None,
        meta: Mapping[str, str] | None = None,
    ) -> None:
        object.__setattr__(self, "vertex_labels", tuple(vertex_labels))
        object.__setattr__(self, "edge_labels", tuple(edge_labels))
        object.__setattr__(self, "property_indexes", tuple(property_indexes))
        object.__setattr__(self, "generated_columns", tuple(generated_columns))
        object.__setattr__(self, "label_hierarchy", dict(label_hierarchy or {}))
        object.__setattr__(self, "meta", dict(meta or {}))


def is_schema_drift(exc: BaseException) -> bool:
    """True when an error means "the server does not know this tenant/label".

    That happens when the tenant was unregistered, or re-registered elsewhere
    with ``force_shrink`` and fewer labels. Re-registering fixes it.
    """
    if not isinstance(exc, GraphosError):
        return False
    if exc.status_code not in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND):
        return False
    detail = str(exc.detail).lower()
    return "not registered" in detail or "does not exist" in detail


class GraphosPool:
    """Lazily created, registered :class:`GraphosClient` per tenant.

    Every client shares ``base_url``, the credentials (``auth``, ``token``, or
    ``GRAPHOS_USER`` / ``GRAPHOS_PASSWORD``, signed in once for all of them)
    and the client options. A tenant is registered with ``schema`` the first
    time it is used; that needs ``create_tenants`` on a pattern its name
    matches (or admin). Concurrent first uses of the same tenant register it
    once.

    Each registration is :meth:`GraphosClient.ensure_tenant` with
    ``on_drift="warn"``: what was changed in the tenant elsewhere (in the
    web UI, by another program) is logged on ``graphos_client.drift``
    before the schema is registered.  A tenant whose schema is managed by
    someone else refuses it (``GraphosError`` 409, ``code ==
    "schema_managed"``) when it would change more than ``display``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        schema: TenantSchema,
        token: str | None = None,
        auth: httpx.Auth | None = None,
        **client_options: Any,
    ) -> None:
        self._base_url = base_url
        self._token = token
        # One sign-in shared by every tenant's client.
        self._auth = auth if auth is not None or token else from_environment()
        self._schema = schema
        self._options = client_options
        self._clients: dict[str, GraphosClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> GraphosPool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _lock(self, tenant: str) -> asyncio.Lock:
        return self._locks.setdefault(tenant, asyncio.Lock())

    async def client(self, tenant: str) -> GraphosClient:
        """The registered client for ``tenant``, creating it on first use."""
        cli = self._clients.get(tenant)
        if cli is not None:
            return cli
        async with self._lock(tenant):
            cli = self._clients.get(tenant)
            if cli is not None:
                return cli
            cli = GraphosClient(
                base_url=self._base_url, tenant=tenant, token=self._token, auth=self._auth,
                **self._options,
            )
            try:
                await self._register(cli, tenant)
            except BaseException:
                await cli.close()
                raise
            self._clients[tenant] = cli
            return cli

    async def reregister(self, tenant: str) -> GraphosClient:
        """Register ``tenant`` again (idempotent on the server) and return its client."""
        cli = await self.client(tenant)
        async with self._lock(tenant):
            await self._register(cli, tenant)
        return cli

    async def call(
        self, tenant: str, op: Callable[[GraphosClient], Awaitable[T]],
    ) -> T:
        """Run ``op(client)``; on schema drift, re-register once and retry.

        Any other error, or drift that survives re-registration, is raised.
        """
        cli = await self.client(tenant)
        try:
            return await op(cli)
        except GraphosError as exc:
            if not is_schema_drift(exc):
                raise
        cli = await self.reregister(tenant)
        return await op(cli)

    async def forget(self, tenant: str) -> None:
        """Close and drop the client for ``tenant`` (e.g. after unregistering it)."""
        cli = self._clients.pop(tenant, None)
        self._locks.pop(tenant, None)
        if cli is not None:
            await cli.close()

    async def close(self) -> None:
        """Close every client the pool created."""
        clients, self._clients = list(self._clients.values()), {}
        self._locks.clear()
        for cli in clients:
            await cli.close()

    async def _register(self, cli: GraphosClient, tenant: str) -> None:
        s = self._schema
        await cli.ensure_tenant(
            on_drift="warn",
            vertex_labels=s.vertex_labels,
            edge_labels=s.edge_labels,
            property_indexes=s.property_indexes,
            generated_columns=s.generated_columns,
            label_hierarchy=s.label_hierarchy,
            meta=s.meta,
        )
