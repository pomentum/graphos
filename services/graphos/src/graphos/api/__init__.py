"""HTTP routers.

One module per topic: ``health`` (/healthz, /readyz), ``info`` (/v1/info),
``tenants`` (/v1/tenants), ``graph`` / ``bulk`` / ``snapshot`` (data plane),
``communities`` / ``louvain`` (community detection), ``cache`` (K/V cache)
and ``db`` (operational introspection). The full catalogue is in
``docs/guide/concept.md`` §2.5.1.

Admin scope is enforced by the token store (token has an ``admin: true``
attribute) rather than by URL prefix — there is no ``/admin`` segment in
the surface. Future operational endpoints (DB stats, vacuum, slow-query
introspection) follow the same convention: live under a topical prefix
(``/v1/db/...``), gated by admin-scoped tokens.
"""
