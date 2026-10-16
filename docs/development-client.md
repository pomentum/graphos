# Developing the Python client

How to work on `graphos-client`, the async Python client in
`clients/python/`. The rules for the whole repository (docs, test stacks,
git, privacy) are in [`development.md`](development.md); they apply here too.
Its users' guide is the [client README](../clients/python/README.md).

## 1. Where to look

Paths are under `clients/python/`.

| Question | Look in |
|---|---|
| A method, its URL, retries and errors | `src/graphos_client/_client.py` (`GraphosClient`) |
| Request and response models | `src/graphos_client/_models.py` |
| Sign-in and token renewal | `src/graphos_client/_auth.py` (`PasswordAuth`, `BearerAuth`) |
| One client per tenant, re-registration | `src/graphos_client/_pool.py` (`GraphosPool`, `TenantSchema`) |
| Errors raised | `src/graphos_client/_errors.py` |
| What the package exports | `src/graphos_client/__init__.py` |
| Runnable examples | `examples/0*_*.py` |
| The service side of a model | `services/graphos/src/graphos/contracts/` |

## 2. Setup, test, lint

```bash
pip install -e "clients/python[dev]"
ruff check clients/python/src clients/python/tests clients/python/examples
pytest clients/python/tests
```

The tests need no server: they use httpx's `MockTransport` and assert the
request the client builds and the model it parses. To run an example
against a running stack, set `GRAPHOS_URL` (default
`http://127.0.0.1:8080`), `GRAPHOS_USER` and `GRAPHOS_PASSWORD`.

## 3. Rules

- **Published on its own.** The client depends on `httpx` and `pydantic`
  only, never on the `graphos` service package. Keep it that way.
- **Models mirror the service 1:1.** `_models.py` is a copy of
  `graphos.contracts`; when a contract changes, the model changes in the
  same commit. A field the service added recently gets a default (`None`
  or empty), so the client still reads older servers.
- **Async only** (`asyncio` + `httpx`), one pooled `httpx.AsyncClient` per
  client, one tenant per client.
- **Retries only what is safe to repeat.** Network errors and 5xx are
  retried with backoff. A call that is long or must not run twice (a
  recompute, an import, a Cypher query, a job submission, a delete with
  data) passes `idempotent=False`. A `504` and 4xx errors are never retried.
- **Errors:** a non-2xx response raises `GraphosError`; a 503 raises
  `GraphosNotReady`.
- **Every public name is exported** from `__init__.py`. The package ships
  `py.typed`; every function is annotated.

## 4. Code conventions

- **Ruff** with the config in `clients/python/pyproject.toml`: line length
  100, target `py311`, rules `E F I B UP SIM RET PL` (`PLR0913` ignored:
  methods mirror the API's parameters).
- Module docstrings, typing and comments follow the service's conventions
  ([development-service.md §4](development-service.md#4-code-conventions)).
- Tests live in `clients/python/tests/test_<area>.py`; names say what is
  checked. A new method gets a test of its request and of its parsed
  response.
- Examples read their settings from the environment and use the neutral
  example tenants (`acme`, `library`, `team_*`, `demo`). Lint covers them.

## 5. When the client changes

Besides the shared list in [development.md §3](development.md#3-docs-discipline):

- the README's "What the client covers" table lists every method; "Not
  wrapped" lists what is left to plain HTTP;
- the README's examples table lists every script in `examples/`;
- a change in retries or errors updates the README's "Behaviour" section;
- a change to what the client promises (async only, its dependencies, what
  it retries) gets an [ADR](adrs/README.md) first.

## 6. Release

The client shares its version with the service and the UI
(`node tools/release/version.mjs <version>`). It is published to PyPI by
hand: the `graphos-client-release` workflow, run from a version tag
(`.github/workflows/client-release.yml`). A version tag does not publish it.
Never tag or publish without the owner's approval
([development.md §6](development.md#6-git)).
