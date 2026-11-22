# ADR-0015: graphos-api drains on SIGTERM, and hands back what it could not finish

- **Status:** Accepted (2026-11-22)
- **Scope:** deployment and runtime: how graphos-api stops (`__main__`, a new `graphos.drain`),
  its settings, the background jobs (`graphos_meta.jobs`, the `Job` contract), the change-feed
  stream, the database connections' settings, the Python client, the release compose file
- **Related:** concept.md §2.4 (deployment), §2.5.7 (failure modes), §2.6 (background jobs),
  §4 (write paths), §4.1 (change feed), §8; usage.md (change feed, jobs);
  [ADR-0012](ADR-0012-database-setup-at-start.md) (start-up, the other end of the lifecycle)

## Context

A platform stops graphos-api by sending SIGTERM, waiting a grace period, then sending SIGKILL.
Every redeploy, upgrade, scale-down and node drain goes through it. The owner's requirements:

- a) block new requests;
- b) complete the running ones;
- c) terminate after a maximum time that is configurable;
- d) resume the unfinished operations when the stop came from that time running out.

### What graphos-api does today (0.16.0)

From the code:

- **The process.** The image's entrypoint ends in `exec python -m graphos`: Python is PID 1 and
  receives the signal itself. `graphos.__main__.serve` calls `uvicorn.run(...,
  timeout_graceful_shutdown=GRAPHOS_SHUTDOWN_GRACE_S)` (5 s), added so that change-feed streams,
  which never end by themselves, do not hold a stop until the kill.
- **Uvicorn 0.54.0** (the version in the image) on SIGTERM, from its source (`uvicorn/server.py`,
  `protocols/http/httptools_impl.py`): it sets `should_exit`; its main loop sees it within 0.1 s
  and calls `shutdown()`, which **closes the listening socket first**, closes idle connections,
  marks in-flight ones `keep_alive = False` (they answer with `Connection: close`), waits up to
  `timeout_graceful_shutdown`, then cancels the request tasks still running, then runs the
  lifespan's shutdown. Only a second **SIGINT** forces the exit; a second SIGTERM changes nothing.
  After it returns, uvicorn restores the original signal handlers and **raises the caught signal
  again** (`capture_signals`), so the exit status comes from that signal, not from graphos.
- **Readiness.** `/readyz` answers 503 before the lifespan has finished and when the database
  does not answer; it knows nothing of a stop. `/healthz` never touches the database and is what
  the compose healthcheck calls.
- **Background jobs** (`graphos.jobs`). `JobRunner.stop()` runs in the lifespan's shutdown, that is
  *after* uvicorn's HTTP wait: until then the runner keeps claiming queued jobs, so a stopping
  process can start a job it has no time to run. `stop()` cancels the running jobs and records
  them `failed` `{"status": 503, "detail": "interrupted: the server was shutting down"}`. A job
  whose heartbeat stops (its process died) is failed by any replica after three heartbeats (30 s).
  Nothing is re-run.
- **The job kinds** (`JOB_KINDS` in `app.py`) and how each commits:

  | Kind | Commits | Safe to run again from the start |
  |---|---|---|
  | `recompute_communities` | partition computed in a child process, written back in one transaction (`writes.set_node_communities`) | yes: nothing is written before that commit; Leiden starts from the stored partition (ADR-0011) |
  | `compute_metrics` | one transaction, only the values that changed (`writes.set_node_metrics`) | yes: a re-run on an unchanged graph writes nothing |
  | `resolve_edges` | one `INSERT … ON CONFLICT DO NOTHING` per edge type; reconcile mode in one transaction | yes: an edge already made is not made twice |
  | `check_properties` | writes nothing | yes |
  | `rename_property` | in batches (`writes.rename_property_batch`) | yes: asking again resumes and finishes it (`schema_rename.py`) |
  | `vacuum` | `VACUUM` / `ANALYZE`, no data | yes |

- **Synchronous calls of the same kinds** (`jobs.run_tracked`, `sync = true`) are listed while the
  caller waits; cancelled, they record `failed` 503 "interrupted: the request was cancelled" and
  re-raise.
- **Writes.** Every write is an upsert keyed by id (nodes) or by type and ordered ends (edges), and
  records its change in the same transaction (DEVELOPMENT.md §3). `:bulk`, `:csv` and
  `snapshot-import` flush every 500 (5,000) rows per label or edge key, **each flush its own
  transaction**: a request cut half-way leaves its earlier flushes written, and resending the
  whole request is safe.
- **Change-feed streams** send `retry: 2000` and every event's cursor as its `id:`; the cursor is
  a database position, so a client that reconnects with `Last-Event-ID`, to any replica, misses
  nothing. The stream waits in `ChangeFeed.wait(tenant)` between pages.
- **MCP** runs the MCP SDK's Streamable HTTP transport with `stateless=True,
  json_response=True`: each call is one ordinary request, with no session to drain.
- **The middleware stack**, outermost first: `TrustedHost` (only when `GRAPHOS_ALLOWED_HOSTS` is
  set), then `RequestContext`, `RateLimit`, `Metrics` and `Audit`. The last four are Starlette
  `BaseHTTPMiddleware`, which runs the handler in an anyio task group.
- **Database connections** set `statement_timeout`, `jit` and `plan_cache_mode` at start
  (`db.create_pool`); asyncpg's reset on release (rollback, `pg_advisory_unlock_all`,
  `RESET ALL`) is kept on purpose.
- **Several replicas** share everything through the database: jobs are claimed with
  `FOR UPDATE SKIP LOCKED`, the registry and caches follow `NOTIFY` (concept.md §2.4).
- **The Python client** retries network errors and 5xx (but 504 and 507) three times, after
  0.5, 1 and 2 s, ignoring `Retry-After`; bulk upserts send a byte body and are retried, a
  snapshot import is not (`idempotent=False`). `stream_changes` reconnects with back-off and
  `Last-Event-ID`. The web UI's `ChangeStream` ignores events other than `change` and `gone`,
  and reconnects one second after a stream ends.

### What that does to clients today (measured)

A prototype server run with the image's own uvicorn, starlette and httpx (Evidence, E3) shows what
a stop does now, with the grace at 4 s:

| A client that… | sees |
|---|---|
| opens a connection after SIGTERM, `/readyz` included | **connection refused** |
| sends on a keep-alive connection after SIGTERM | **connection refused** (the idle connection was closed) |
| has a request that ends within the grace | its answer, with `Connection: close` |
| has a request still running at the grace | **`500 Internal Server Error`** (uvicorn cancels the task) |
| follows the change feed | the stream cut at the grace, mid-chunk (`RemoteProtocolError`) |
| is uploading | the connection reset (`ReadError`) |

The process ends by its own re-raised SIGTERM (status −15 outside a container; 0 as PID 1 in one,
where the kernel ignores it). A second SIGTERM is ignored: the full grace is waited.

So today graphos meets a) only by closing its socket, b) for 5 s, c) for HTTP only, and not d).

### The platforms' grace

- Docker documents a default stop timeout of 10 s for Linux containers, then SIGKILL. **Measured**
  on Docker Desktop (engine 29.9.3, Compose 5.7.0): a container that ignores SIGTERM is killed
  after **3.4 s** by `docker stop`, and 3.7 s by `docker compose stop`, when no
  `stop_grace_period` / `-t` is set; with `-t 10`, after 10.4 s. A deployment must therefore never
  rely on the platform's default.
- Kubernetes: `terminationGracePeriodSeconds` defaults to 30 s; a `preStop` hook runs inside it;
  the Pod is taken out of its EndpointSlices *at the same time* as the kubelet starts the
  shutdown, not before, so a load balancer may still send requests for a moment after SIGTERM.

### What a killed process leaves in the database (measured)

With asyncpg 0.32 and PostgreSQL 18 (E4):

- a task cancelled inside `conn.transaction()` mid-statement: the statement is cancelled in 5 ms,
  the transaction rolls back (row and change record both gone), the session's advisory lock is
  released by the pool's reset;
- a batched writer cancelled in its third flush: the first two stay committed, the third is gone;
- **a process killed (SIGKILL) during a statement**: its rows are not visible, but its backend
  **keeps running the statement and holding its locks** until the statement ends (still held 10 s
  later), because PostgreSQL does not notice a vanished client while a query runs. A
  whole-graph job's statement may run for 600 s (communities) or 1800 s (edge resolution), and
  holds the tenant's whole-graph advisory lock all that time. With
  `client_connection_check_interval = 1s` on the connection the lock was released within 2 s.

## Decision

graphos-api drains when it receives SIGTERM or SIGINT, in phases bounded by one deadline, and
hands back what it could not finish so that it resumes.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `GRAPHOS_SHUTDOWN_GRACE_S` | `8` (was `5`) | The drain's maximum: from the signal to the process's exit. Kept by name; its meaning widens from "open requests" to the whole drain. Must be at least 2. |
| `GRAPHOS_DRAIN_DELAY_S` | `0` | Seconds after the signal during which graphos still serves every request while `/readyz` already says it is draining, so that load balancers stop sending it requests before it refuses any. Behind a load balancer or on Kubernetes, `5`. Must be less than the grace minus the reserve; graphos refuses to start otherwise. |

Fixed, not settings: the **reserve**, a quarter of the grace and at most 5 s (2 s at the
default), kept for handing work back and exiting; `Retry-After: 2`; a job is put back in the
queue **twice at most, and failed on its third interruption**; `client_connection_check_interval`
of **5 s** on every connection graphos opens.

**The platform's grace must be longer than graphos's**, by at least 5 s for the exit:
`stop_grace_period` / `terminationGracePeriodSeconds` ≥ `GRAPHOS_SHUTDOWN_GRACE_S` + 5. graphos
logs both of its settings at start. The default of 8 s fits Docker's documented 10 s and
Kubernetes' 30 s, but not what Docker Desktop was measured to do, so the docs tell every
deployment to set its platform grace explicitly.

- **Release compose file:** `GRAPHOS_SHUTDOWN_GRACE_S: ${GRAPHOS_SHUTDOWN_GRACE_S:-30}` and
  `stop_grace_period: 40s` on graphos-api.
- **Kubernetes** (installation.md): `GRAPHOS_DRAIN_DELAY_S=5`, `GRAPHOS_SHUTDOWN_GRACE_S=30`,
  `terminationGracePeriodSeconds: 40`, a readiness probe on `/readyz`, a liveness probe on
  `/healthz`, and no `preStop` sleep: the delay does that inside the process, where graphos's logs
  and metrics can see it.

### The phases

*T* is the signal; *grace* is `GRAPHOS_SHUTDOWN_GRACE_S`; the cut is at *T + grace − reserve*.

**1. Draining (at *T*).**

- `/readyz` answers `503 {"status": "not_ready", "reason": "draining"}`. `/healthz` stays `200`:
  the process is alive, and a failing liveness probe would only get it killed sooner.
- The job runner stops claiming, and marks the jobs it runs `draining_since = now()`.
- Every response from now on carries `Connection: close` (uvicorn then closes the connection after
  it), so clients come back on new connections, which a load balancer sends elsewhere.
- Requests are still served, job submissions included: a queued job is stored in the database
  and another replica, or this one's next start, runs it.

**2. Refusing (at *T + `GRAPHOS_DRAIN_DELAY_S`*).** This is a).

- graphos **keeps listening** and answers every new request, except `/healthz`, `/readyz` and
  `/metrics`, with:

  ```http
  HTTP/1.1 503 Service Unavailable
  Content-Type: application/problem+json
  Retry-After: 2
  Connection: close

  {"type": "about:blank", "title": "Shutting down", "status": 503,
   "detail": "graphos is shutting down; send the request again", "code": "draining"}
  ```

  A client of a single instance gets an answer it can act on rather than "connection refused",
  which looks the same as graphos being down. Behind a load balancer, the retry two seconds later
  reaches another replica. Refusals are counted, not audited: nothing happened.
- **Change-feed streams end**: each sends `event: draining` with
  `data: {"reason": "draining"}` and closes. The Python client reconnects at once with
  `Last-Event-ID`; the web UI ignores the event and reconnects after one second, as it does after
  any end. Nothing is lost: the cursor is in the database.
- MCP needs nothing of its own (stateless).

**3. Finishing (until the cut).** This is b). Requests in flight and running jobs run on. When
nothing is in flight and no job runs, the drain goes straight to the exit.

**4. Handing back (at the cut, or at once on a second signal).** This is c) and d). Everything
still running is cancelled. A database transaction is atomic: a write and its change record are
both committed or both rolled back (E4), so no write is left half-done whatever moment the cut
hits. What each kind of work becomes:

| Still running | What graphos does | What the client sees |
|---|---|---|
| A background job | Its task is cancelled by the existing cancel path (statement cancelled, a community or metrics child process terminated, transaction rolled back). The row goes back to **`queued`**: `attempts` + 1; `started_at`, `heartbeat_at`, `progress` and `draining_since` cleared; then `NOTIFY graphos_jobs`. A job whose `cancel_requested` is set becomes `cancelled` instead. | `GET /v1/jobs/{id}`: same id, status `running` → `queued` → `running` → `succeeded`, `attempts: 1`. `wait_for_job` keeps waiting. |
| A job already put back twice (its third interruption) | Recorded `failed`: `{"status": 503, "detail": "interrupted: graphos stopped 3 times while the job ran; raise GRAPHOS_SHUTDOWN_GRACE_S or run it when no stop is due"}`. | A failed job with that reason. |
| A job of a kind not marked resumable (none today) | Recorded `failed` 503 "interrupted: the server was shutting down", as today. | As today. |
| A synchronous call of a job kind (`run_tracked`) | Cancelled the same way; its row is recorded `failed` 503 "interrupted: the server was shutting down". Not queued: nobody would collect the result. | `503` + `Retry-After` + `code: "draining"`. Sending it again is safe: every kind runs again from the start. |
| An upload (`:bulk`, `:csv`, `snapshot-import`) | Cancelled. Flushes committed before the cut stay; the flush in progress rolls back. | If the client had sent its whole body: the `503`. If it was still sending: a reset connection (`ReadError`, E3). Either way, sending the whole request again is safe (upserts); the Python client does it for bulk upserts by itself. |
| Any other request (read, export, console query, single write) | Cancelled. | `503` + `Retry-After` if the response had not started; a connection closed mid-body otherwise (an export ends without its last chunk). Reads and upserts are safe to send again; a `PATCH` with `If-Match` sent again answers `412` if the first one had committed; a delete, `404`. |
| Background tasks (the node-properties backfill, change-feed pruning) | Cancelled. Both are idempotent and continue at the next start. | Nothing. |

A cancellation that reaches a write while its `COMMIT` is on the wire can leave it committed and
its answer lost. Any network failure does the same; the table's "sent again" column is the way
out, as it is today.

**5. Exit (by *T + grace*).** uvicorn is told to stop, with `timeout_graceful_shutdown` set to
what is left; it closes the socket and runs the lifespan's shutdown, which closes the change
feed, the listener, the job runner (whose `stop()` hands back whatever is left by the same rule
as phase 4, so a stop that did not come through the drain hands back too), the console pool and
the main pool. A watchdog thread, armed by the first signal, exits the process (`os._exit(1)`)
at *T + grace* if it is still there, so c) holds even when a step hangs or the event loop is
stuck. The exit status is `0` after a drain whose steps all completed, `1` otherwise (a job
could not be put back, a step failed or overran, or the watchdog fired), and graphos no longer
re-raises the signal.

### When the platform kills graphos before it is done

A grace set too short on the platform side, or a process killed for memory, skips the hand-back:

- **Jobs.** Any replica's sweep (and the process's own next start) finds the job `running` with a
  stale heartbeat. If its row carries `draining_since`, the stop came from a drain, not from the
  job: the sweep puts it back in the queue by the same rule (put back twice at most). Without
  that mark, it fails it as today: a job that killed its own process (memory) would kill the next
  replica too.
- **Statements.** `client_connection_check_interval = 5s`, set on every connection graphos opens
  (the main pool, which the listener draws from too, and the console's pool), makes PostgreSQL end a killed process's statement within about 5 s
  and release its locks, instead of running it to the end with the tenant's whole-graph lock
  held, which would refuse the job's next run with `409`.

### Several instances behind one address

Each replica drains on its own; nothing is coordinated between them. A rolling update relies on
the platform taking a draining replica out of rotation, which `/readyz` and
`GRAPHOS_DRAIN_DELAY_S` arrange. What a client had on a draining replica continues on another:
change-feed streams by `Last-Event-ID`, jobs from the queue under the same id, refused requests
after `Retry-After`. A hand-back sends `NOTIFY graphos_jobs`; every runner listens on it and
claims at once instead of at its next poll (`GRAPHOS_JOB_POLL_S`, 5 s). Rate-limit buckets are
per replica (concept.md §2.4): clients moving over spend the other replicas' buckets.

### What a drain reports

- **Logs**, one JSON line each: `drain started` (`signal`, `grace_s`, `delay_s`, `in_flight`,
  `jobs_running`); `drain refusing` (`in_flight`); per job, `job handed back` (`job_id`, `kind`,
  `outcome`: `queued` | `failed` | `cancelled`, `gone` when another process's sweep took it
  first, `error` when it could not be written); `drained` at the end (`waited_s`, `cut`:
  `requests`, `streams`, `jobs_queued`, `jobs_failed`, `jobs_cancelled`; `exit_code`). A second
  signal logs `drain cut short`; the watchdog logs `drain did not finish in time; exiting`; the
  sweep logs `jobs stopped by a drain put back in the queue` (`job_ids`). graphos logs both
  settings in its `graphos ready` line.
- **Metrics** (`/metrics` keeps answering): `graphos_draining` (gauge, 0 or 1),
  `graphos_requests_in_flight` (gauge, every request but `/healthz`, `/readyz` and `/metrics`,
  not only during a drain), `graphos_drain_refused_total` (counter) and
  `graphos_jobs_handed_back_total{kind, outcome}` (counter, the sweep's put-backs included).

### What changes in the contract

- `Job` gains `attempts` (integer, `0` for a job never handed back): "times a stop of graphos-api
  put the job back in the queue". No new status. `draining_since` stays internal.
- The change-feed stream may end with `event: draining`.
- Any route may answer `503` with `code: "draining"` and `Retry-After`.
- `GRAPHOS_DRAIN_DELAY_S` is new; `GRAPHOS_SHUTDOWN_GRACE_S` changes default and meaning.

### Building it

| Where | What |
|---|---|
| `graphos/drain.py` (new) | `Drain`: the phase (`serving`, `draining`, `refusing`, `cutting`, `done`), the requests in flight (task → path), the tasks it cuts, the outcome. `signal(name)` sets `draining` at once and starts the phases as a task; a later call cuts them short. Each hook runs bounded and guarded: one that fails or overruns sets exit status 1 and the steps after it still run; stopping the job runner runs beside the phases, so a database that does not answer cannot hold the refusals back. `DrainMiddleware`, a pure ASGI middleware: refuses with the 503 once refusing (exempt paths aside), adds `Connection: close` once draining, registers each request's task, and catches the drain's own cancellation. It recognises its cut by **marking the tasks it cancels**, not by `Task.cancelling()`: under `BaseHTTPMiddleware` the count is back to 0 when the `CancelledError` reaches it (E3), and the request would end in a 500. It then clears the count (`uncancel()` until 0) and sends the 503 if the response has not started. `DrainHooks` is what the lifespan gives it. One `Drain` per process, made by `create_app` (`app.state.drain`) and found by `drain.current()`. |
| `graphos/__main__.py` | `serve()` builds `uvicorn.Config(..., factory=True)` and runs `GraphosServer`, a `uvicorn.Server` that keeps the drain of the app it loaded (`server.drain`) and whose `handle_exit` hands a signal to `Drain.signal` (through `loop.call_soon_threadsafe`) instead of setting `should_exit`, and does not record it, so it is not raised again; the first signal arms the watchdog thread from that loop callback, never from the signal handler, where starting a thread could deadlock (`watchdog=False` for a process that goes on after the server, as tests are; disarmed once the server has run). The drain sets `should_exit` at the end (`on_done`), giving uvicorn's own wait half of what is left and the lifespan's shutdown the other half. A signal before the server has started takes uvicorn's way. Exit status 3 when the server never started (as `uvicorn.run`), else the drain's. |
| `graphos/app.py` | `DrainMiddleware` inside `TrustedHost` and outside every `BaseHTTPMiddleware`. The lifespan gives the drain its hooks (`runner.begin_drain`, `feed.wake_all`, `runner.hand_back_all`, `runner.running`) and subscribes the runner to `graphos_jobs`. `JOB_KINDS`: `resumable=True` on the six kinds, with the reasons from the table in Context. |
| `graphos/api/health.py` | `/readyz` answers `reason: "draining"` once the drain has begun, before any database check. |
| `graphos/jobs.py` | `JobKind.resumable: bool = False`. `JobRunner.begin_drain()` (stop claiming, set `draining_since`), `hand_back_all()` (cancel the jobs inside their kind's work, never while one records its outcome, and put each back by the `queued` / `failed` / `cancelled` rule, under `FOR UPDATE` on a row still `running`), `running()`; `stop()` hands back instead of failing. The hand-back waits with `asyncio.wait`, so a wait cut short by the drain's budget does not cancel the jobs a second time while they record their outcome. A run is named by its `attempts`, returned by the claim: the outcome, the heartbeat and the hand-back write only `WHERE attempts = $n`, so a slow old process cannot write over a run another replica claimed after a put-back. Claiming stops by the loop's own check rather than by cancelling it mid-claim (cancelled after 5 s if the database hangs), and every caller waits for the same loop. A user's cancel reaches a job only inside its work, never while it records its outcome. `_sweep` puts back stale `running` jobs that carry `draining_since` and are of a resumable kind this process knows, and ends the others by the hand-back's rule (`cancelled` if asked, else `failed` 503 on the third interruption), before failing the rest. `run_tracked` records "interrupted: the server was shutting down" when the drain cut it, reading and writing the job row inside `anyio.CancelScope(shield=True)`: the cut reaches it through the request's task group, which cancels every later await (E3), and the row would stay `running`. `handing_back()` tells a job's own code that a stop is interrupting it to hand it back. |
| `graphos/schema_rename.py` | A property rename interrupted to be handed back is not marked `failed`: it stays `running` and goes on where it stopped when its job runs again. If the hand-back fails it instead, the rename stays `running`, as after a crash, and asking again resumes it. |
| `graphos/db.py`, `graphos/console.py` | `attempts integer NOT NULL DEFAULT 0` and `draining_since timestamptz` on `graphos_meta.jobs`; `client_connection_check_interval: "5000"` in both pools' `server_settings`. |
| `graphos/changes.py`, `api/changes.py` | `ChangeFeed.wake_all()` (public now) wakes every stream; `_events` ends with `event: draining` once the drain refuses. |
| `graphos/contracts/jobs.py` | `attempts: int = 0`, described. |
| `graphos/config.py` | `shutdown_grace_s` default 8, `ge=2`; `drain_delay_s` default 0; `drain_reserve_s()`; a validator for the delay against the grace less the reserve. |
| `observability/metrics.py` | The four metrics. |
| Python client | `Job.attempts: int = 0`; `_request` waits `max(back-off, Retry-After)` (at most 30 s) before retrying a 503; `stream_changes` reconnects without back-off after `event: draining`. README: Behaviour. |
| Web UI | Nothing (its stream already reconnects; the jobs list may show `attempts` later). |
| `docker-compose.graphos.yml` | `stop_grace_period: 40s`; `GRAPHOS_SHUTDOWN_GRACE_S: ${GRAPHOS_SHUTDOWN_GRACE_S:-30}`. |
| `tests/_live.py` | `graphos_server(app)`: the app served by `GraphosServer`, for tests that send it a signal. |

## Decision options

| Option | Taken? | Why |
|---|---|---|
| **Readiness first, then 503 while still listening, then hand back at a deadline** | Yes | Meets a)–d) on one instance and on many, with what graphos already has: job rows, the cancel path, upserts, database cursors. Prototyped (E3). |
| Keep today's stop | No | Refuses connections at once, answers 500 to the requests it cuts, fails jobs and runs nothing again (E3). |
| Close the socket at the signal, as uvicorn does | No | Clients of a single instance see "connection refused", the same as an outage, and get no `Retry-After`; on Kubernetes the endpoint is removed only as the signal arrives, so requests still come in. |
| A `preStop` sleep instead of `GRAPHOS_DRAIN_DELAY_S` | No | Compose has none, and the sleep happens outside graphos, so its logs and metrics cannot say it is draining. |
| Wait for running jobs however long they take | No | Fails c): a recompute or an edge resolution runs for minutes. |
| Checkpoint jobs and resume them mid-way | No | Every kind commits once at the end or in idempotent batches, so running it again from the start loses only time, and `rename_property` already continues where it stopped. A future kind that cannot run twice declares `resumable=False`. |
| Put back every job whose heartbeat stopped | No | A job that killed its process for memory would kill one replica after another. Only a job marked by a drain is put back. |
| Answer a cut upload with how far it got (`lines_committed`), to resend only the rest | No | httpx 0.28 does not read an answer sent while it is still uploading: it reports `ReadError` (E3). The offset would reach only clients that had finished sending, and resending everything is safe. |
| Let uploads stop by themselves at their next flush instead of being cancelled | No | Same reason: the client cannot read the answer, and a transaction rolled back is as safe as one not started. One rule for every request is simpler to build and to test. |
| A new name for the grace (`GRAPHOS_DRAIN_TIMEOUT_S`) | No | Same meaning, widened; a rename costs every operator a change (ADR-0009) for nothing. |
| A default grace of 30 s | No | Longer than Docker's documented 10 s: a deployment that sets nothing would be killed mid-drain, the worst outcome. The release compose file raises both together. |
| `Retry-After` from the time left in the drain | No | Behind a load balancer the retry goes to another replica at once; a long `Retry-After` would only hold those clients. With one instance the caller's own retries decide. 2 s matches the stream's `retry: 2000`. |
| How many interruptions a job survives, as a setting | No | A job interrupted three times did not fit in three drains; a fourth try moves it again. The setting to change is the grace. |
| A drain started by a request (`POST /v1/admin:drain`) | No | Every platform stops a process with SIGTERM; graphos has no deploy of its own that would need a drain without a stop. |
| A resume point for exports | No | Exports only read: a cut one runs again. Paged reads (`edges:scan`, `changes`) already resume by cursor. Listed in concept.md §8. |
| uvicorn's default exit (re-raise the signal) | No | The status would say "killed by SIGTERM" after a clean drain, and nothing about a hand-back that failed. |

## Evidence

**E1. The code** (read, 0.16.0): `docker-entrypoint.sh` (`exec python -m graphos`);
`__main__.serve`; `config.py` (`shutdown_grace_s` 5.0, `job_heartbeat_s` 10, `job_poll_s` 5);
`app._lifespan` (the runner stopped in the `finally`); `jobs.JobRunner` (`_loop`, `_claim`,
`_sweep`, `_run`, `stop`) and `jobs.run_tracked`; `JOB_KINDS`; `writes.set_node_communities`,
`graph_metrics` (one transaction), `writes.rename_property_batch`; `api/health.py`;
`api/changes.py` (`_events`); `mcp_server.py` (`stateless=True, json_response=True`);
`db.create_pool` (`server_settings`, asyncpg's reset kept); `db.py` (the jobs table and its
unique indexes on `queued`/`running`); the middleware in `app.create_app`; concept.md §4 (one
transaction per flush); the client's `_request`, `stream_changes`, `wait_for_job`, `Job`; the UI's
`api/changeStream.ts`; `docker-compose.graphos.yml` (no `stop_grace_period`).

**E2. Library source**, copied out of the published 0.16.0 image, whose versions are uvicorn
0.54.0, starlette 1.10.0, httptools 0.9.0, h11 0.16.0, asyncpg 0.32.0, httpx 0.28.1, anyio
4.16.0, fastapi 0.162.6, mcp 2.3.0:

- `uvicorn/server.py`: `handle_exit` (a second SIGINT forces; SIGTERM only sets `should_exit`),
  `main_loop` (checks every 0.1 s), `shutdown` (socket closed first, then connections, then
  `wait_for(..., timeout_graceful_shutdown)` read from the config at that moment, then cancel, then
  lifespan), `capture_signals` (re-raises the caught signals); `uvicorn/main.py` (`run` exits 3 when
  the server never started).
- `protocols/http/httptools_impl.py`: `shutdown()` closes an idle connection and sets
  `keep_alive = False` on a busy one; a response that carries `Connection: close` sets it too, so
  the connection closes after the response.
- `asyncpg/transaction.py`: `__aexit__` rolls back on any exception, cancellation included;
  `asyncpg/pool.py` and `connection.py`: release waits for a cancellation in progress, then resets
  (rollback, advisory locks released), and terminates the connection if the reset fails.
- PostgreSQL 18 (`postgres --describe-config`): `client_connection_check_interval` is of context
  `user`, so graphos's own role may set it.

**E3. A prototype of the drain**, run in a throwaway container from the 0.16.0 image (its uvicorn,
starlette and httpx), next to plain uvicorn as graphos runs it today. Grace 4 s, delay 1 s:

| | uvicorn as today | prototype drain |
|---|---|---|
| `/readyz` after SIGTERM | connection refused | `503 {"reason": "draining"}` at once |
| new / keep-alive request during the delay | connection refused | `200`, `Connection: close` |
| new request after the delay | connection refused | `503`, `Retry-After: 2`, `code: "draining"`; `/healthz` `200` |
| request that ends in time (2.5 s) | `200`, `Connection: close` | `200`, `Connection: close` |
| request still running at the cut (30 s) | `500` at 4.5 s | `503` + `Retry-After` at 3.3 s |
| the same, behind three `BaseHTTPMiddleware` layers | — | `500` when the middleware relied on `Task.cancelling()` (0 by then); **`503`** once it marks the tasks it cuts |
| an `await` in the handler after the cut (its cleanup) | completes | under `BaseHTTPMiddleware`, **cancelled again** (anyio delivers the cancellation at every await); completes inside `anyio.CancelScope(shield=True)` |
| change-feed stream | cut at 4.5 s, `RemoteProtocolError` | ended at 1.6 s with `event: draining`, cleanly |
| upload in progress at the cut | `ReadError` | `ReadError`: httpx does not read the early 503 |
| new upload while refusing | connection refused | `ReadError` (same reason) |
| a second SIGTERM | ignored; 20 s grace waited | cut at once, `503`, exit after 2 s |
| exit status (child process) | −15 | 0 |
| `docker stop` with the server as PID 1 | 0 | 0 |

**E4. The database**, a throwaway PostgreSQL 18.6 container and asyncpg 0.32 from the image:
cancelled mid-statement in a transaction holding a session advisory lock → cancelled in 5 ms,
rows and change record rolled back, lock released, no session idle in transaction; a batched
writer cancelled in its third flush → two flushes kept, the third gone; a process killed during
`pg_sleep(60)` in a transaction holding an advisory lock → rows invisible but lock and backend
still there 0.5, 2, 5 and 10 s later; the same with `client_connection_check_interval = 1000ms`
→ lock released by 2 s.

**E5. The platforms.** Docker's `docker container stop` reference: SIGTERM, then SIGKILL after a
default of 10 s for Linux containers. Measured on Docker Desktop (engine 29.9.3, Compose 5.7.0):
3.4 s (`docker stop`) and 3.7 s (`docker compose stop`) without a timeout set, 10.4 s with
`-t 10`; exit status 137. Kubernetes' Pod termination documentation: a 30 s default grace that
includes `preStop`, and the Pod leaves its EndpointSlices at the same time as the shutdown starts.

**E6. End to end, as built**, on the release compose file with graphos-api built from the
checkout (a compose project of its own), `GRAPHOS_DRAIN_DELAY_S=1`, a property rename over
600,000 nodes running, then `docker compose stop graphos-api`:

- during the delay `/readyz` answered `503 {"reason": "draining"}`, `/healthz` `200`, and a
  request `200` with `Connection: close`; after it, `503` with `Retry-After: 2`,
  `Connection: close` and `code: "draining"`;
- with a grace of 8 s the rename of 200,000 rows finished within it and the process exited 0
  (requirement b);
- with a grace of 4 s the rename was cut: `job handed back … queued`, exit status 0 after 4.6 s,
  the job `queued` with `attempts` 1 and the rename still `running`; after
  `docker compose start graphos-api` the job `succeeded` with `attempts` 1, having rewritten the
  400,000 rows left, and the rename was `done`;
- with a second replica running, the second replica had already claimed the job when the stop
  returned (woken by `NOTIFY graphos_jobs`), and finished it.

**Not measured, and why it does not hold the decision up:** how long a load balancer in front of
graphos takes to stop routing (it depends on the balancer: `GRAPHOS_DRAIN_DELAY_S` is set to its
health-check interval times its failure threshold, and installation.md says so; 5 s is the
starting value for Kubernetes, whose endpoints change as the signal arrives); how long a
hand-back takes with many jobs (one short transaction per job, and `GRAPHOS_JOB_CONCURRENCY` is 1
by default; the hand-back is bounded by the reserve, and a job it misses is requeued by the
sweep).

## Consequences

- Redeploys and upgrades no longer lose jobs: a recompute cut by a stop runs again elsewhere or
  after the restart, under the same id.
- A stopping replica no longer starts jobs it cannot finish.
- During a restart, clients get `503` + `Retry-After` instead of refused connections, and a cut
  request gets `503` instead of `500`.
- Change-feed clients move to another replica as soon as the drain starts.
- A killed process no longer leaves a statement running for minutes with the tenant's whole-graph
  lock held.
- A job may run more than once from the start. Every kind today is safe to; a new kind says
  whether it is (`resumable` defaults to false).
- An upload cut mid-body must be sent again whole; nothing tells the client how far it got.
- An operator who raises `GRAPHOS_SHUTDOWN_GRACE_S` must raise the platform's grace with it; the
  docs say so, the start-up log shows both settings, and every deployment sets its platform
  grace explicitly.
- Docs that change with it: concept.md §2.4, §2.5.7, §2.6, §4.1 and §8 (cut uploads and exports
  start again); usage.md (the 503, `event: draining`, `attempts`); installation.md (stopping
  graphos, compose, Kubernetes); the README's configuration table (both settings); the client
  README's Behaviour; the CHANGELOG.

## Open questions

None. The questions an earlier draft left open are decided above: `Retry-After` (2 s), how
many interruptions a job survives (two; fixed), a drain started by a request (no), jobs whose
process was killed during a drain (put back when marked `draining_since`), a resume point for
uploads and exports (no).

## Acceptance

- **Unit tests** (`tests/test_drain.py`, the app behind its real middleware stack in uvicorn,
  stopped through `GraphosServer.handle_exit`): `/readyz` says `draining` from the signal; during
  the delay requests are served with `Connection: close`; afterwards they get the 503 with
  `Retry-After: 2` and `code: "draining"`, while `/healthz`, `/readyz` and `/metrics` answer and
  the metrics show the drain; a request that outlasts the drain is cut and gets `503`, not `500`;
  the signal is not raised again; a second signal cuts at once; with nothing running the drain
  ends after its delay; the hooks run in order and a running job is waited for until the cut; a
  job that could not be put back, or a failing step, makes the exit status 1 and the later steps
  still run; a change-feed stream ends with `event: draining`; both pools set
  `client_connection_check_interval`; the watchdog is armed from the event loop; a delay not
  below the grace minus the reserve is refused at start. `tests/test_serve.py`: the settings
  `serve()` hands uvicorn, status 3 when the server never started.
- **Database tests** (`tests/test_drain_db.py`): a running resumable job, cut, is `queued` with
  `attempts` 1, `NOTIFY graphos_jobs` is sent, the draining runner claims nothing more, and a
  second runner finishes the job; the third interruption fails it with the 503; a job of a kind
  that may not run twice is failed; a job with `cancel_requested` becomes `cancelled`; a stale
  `running` job with `draining_since` is put back by another runner's sweep, one without it is
  failed, one on its third interruption is failed; a synchronous call cut through HTTP answers
  503 and is recorded `failed` 503; a write cut in its transaction leaves nothing and no open
  transaction; a `:bulk` upload cut mid-body keeps exactly its committed flushes, each with its
  change records; a property rename handed back stays `running` and finishes when its job runs
  again. With `tests/test_jobs.py` unchanged and passing.
- **Client tests:** a 503 with `Retry-After` is retried after it (at most 30 s); `event: draining`
  reconnects without back-off; `Job.attempts` parses, and defaults to 0 against an older server.
- **On the release compose stack** (built from the checkout, its own compose project):
  `docker compose stop graphos-api` during a property rename answers `503 draining` on `/readyz`
  at once and `503` with `Retry-After` after the delay, exits with status 0 within
  `stop_grace_period`, leaves the job `queued` with `attempts` 1, and after
  `docker compose start graphos-api` the job finishes and the rename is `done`; with a second
  replica, that replica takes the job over and finishes it (E6).
- The docs listed under Consequences; ruff and mypy clean.
