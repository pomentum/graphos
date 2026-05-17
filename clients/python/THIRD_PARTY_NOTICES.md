# Third-party notices

graphos is licensed under Apache-2.0 (see `LICENSE` and `NOTICE`). The software listed below is
not part of that grant: it comes from other projects and remains under its own licence. None of
it is vendored in this repository; it is pulled in at build or run time.

## Apache AGE

- **Project:** Apache AGE, https://age.apache.org/ (source: https://github.com/apache/age)
- **Version:** 1.6.0 for PostgreSQL 16 (container image `apache/age:release_PG16_1.6.0`)
- **Licence:** Apache-2.0
- **Used by:** `docker-compose.graphos.yml` (service `graphos-db`) and
  `services/graphos/deploy/db-init/00-extensions.sql`, which runs `CREATE EXTENSION age`.

graphos uses the upstream image unmodified. AGE provides the property-graph storage model and
the openCypher engine that `graphos-api` calls through `cypher()`; graphos does not copy, patch
or redistribute AGE source code.

"Apache", "Apache AGE" and the Apache feather logo are trademarks of The Apache Software
Foundation. graphos is an independent project and is not affiliated with or endorsed by the
Apache Software Foundation.

The Apache AGE image is built on the official PostgreSQL image. PostgreSQL is distributed under
the PostgreSQL Licence (https://www.postgresql.org/about/licence/); the `pg_trgm` and
`pg_stat_statements` extensions graphos enables ship with PostgreSQL under the same licence.

AGE carries its own `LICENSE` and `NOTICE` files, reproduced in its source distribution and at
https://github.com/apache/age. Apache-2.0 text: https://www.apache.org/licenses/LICENSE-2.0

## Python dependencies

Installed from PyPI by `services/graphos/pyproject.toml` and `clients/python/pyproject.toml`,
and baked into the container image built from `services/graphos/Dockerfile`. Each package
carries its own licence file in its distribution.

| Package | Licence | Used by |
|---|---|---|
| fastapi | MIT | service |
| uvicorn | BSD-3-Clause | service |
| asyncpg | Apache-2.0 | service |
| pydantic | MIT | service, client |
| pydantic-settings | MIT | service |
| structlog | MIT OR Apache-2.0 | service |
| prometheus-client | Apache-2.0 AND BSD-2-Clause | service |
| python-igraph | GPL-2.0-or-later | service, **optional** `louvain` extra only |
| httpx | BSD-3-Clause | client |

These are the direct dependencies. Installing them pulls in further packages
(for example starlette, anyio, h11, idna, uvloop, httptools, websockets,
watchfiles, python-dotenv, PyYAML, click, typing-extensions, certifi), each
under its own licence: permissive (MIT, BSD, Apache-2.0) or, for certifi,
the weak-copyleft MPL-2.0 (unmodified, file-level),
and the container image is built on the official `python:3.11-slim` Debian
image, whose packages carry their own licences. Every installed package keeps
its licence file in its distribution metadata inside the image.

**Note on python-igraph.** python-igraph is licensed under the GNU GPL, version 2 or later. It is
not a default dependency: it is installed only with the optional extra (`pip install
"graphos[louvain]"`) or an image built with `--build-arg GRAPHOS_EXTRAS=louvain`. Without it,
graphos runs normally and `POST /v1/{tenant}/recompute-communities` answers `501`. The default
container image published by this repository's workflow does not contain it. If you distribute
an install or image that includes python-igraph, that distribution must comply with the GPL for
the igraph component.
