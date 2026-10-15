# graphos — docs

Start with **[`concept.md`](concept.md)** — the standing reference. It describes graphos as a service: architecture and deployment (including the unprivileged database role the API runs as), the two-layer schema setup, the REST surface (auth, timeouts, observability), the Cypher / SQL boundary, write paths, community detection (Louvain), export, sizing, and known limitations.

- **[`installation.md`](installation.md)** — installing, the first sign-in, users, single sign-on, production settings, upgrading.
- **[`features.md`](features.md)** — capabilities at a glance.
- **[`algorithms.md`](algorithms.md)** — the graph algorithms (Louvain, PageRank, betweenness, components, degree): what each finds and what it is for.
- **[`usage.md`](usage.md)** — worked requests for every endpoint group, limits and errors.
- **[`sso.md`](sso.md)** — single sign-on with any OpenID Connect provider: what to register, settings, troubleshooting.
- **[`../services/graphos-ui/README.md`](../services/graphos-ui/README.md)** — the web UI: running it, its views, how its code and text are organised.
- **[`apache-age.md`](apache-age.md)** and **[`graph_age_practices.md`](graph_age_practices.md)** — Apache AGE lessons and the write-path templates (including why graphos writes every row through COPY-staged SQL rather than Cypher).

**Quickstart for new consumers (any schema):** read concept.md **§2.5.8 — Generic analytics — examples** for `histogram`, `top`, `community-ids`, and `nodes/{id}/properties`, and **§2.5.9 — K/V cache — example** for the `cache/{key}` surface when an aggregate has no natural graph-node anchor. Both sections include curl examples that work for any registered schema — substitute your own labels and properties.

The live API contract is **`GET /openapi.json`** off the running service. The reference Python client lives in [`clients/python/`](../clients/python/).
