"""Settings loaded from environment variables.

All env vars are prefixed `GRAPHOS_`. Secret files are read once at startup
(via the *_FILE convention) and held in memory; SIGHUP triggers a reload
in auth.py (not in this module — config itself is immutable post-construction).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GRAPHOS_",
        env_file=None,                # production: env-only; dev: docker-compose injects
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database (graphos-db) ────────────────────────────────────────────
    db_host: str = "graphos-db"
    db_port: int = 5432
    db_name: str = "graphos"
    db_user: str = "graphos"
    db_password: SecretStr | None = None
    db_password_file: Path | None = None
    db_pool_min_size: int = 2
    db_pool_max_size: int = 16
    # Server-side ``statement_timeout`` applied to every pooled connection.
    # A statement that exceeds it is cancelled by Postgres and the request
    # answers 504.  The few deliberately long operations (Louvain stages,
    # rule-based edge resolution, vacuum) raise it for their own statement.
    statement_timeout_s: float = Field(default=60.0, gt=0)
    # PostgreSQL JIT compilation for graphos's connections.  Off: it showed
    # no gain on the heaviest queries and cost seconds on small ones.
    db_jit: bool = False
    # A btree on ``id`` for every vertex label table (edge tables always get
    # one).  Speeds up resolving edge endpoints (e.g. ``/edges:scan`` pages,
    # ~1 s -> ~60 ms on an 8M-edge type) at a measured 20-30% cost on bulk
    # vertex writes.  Built concurrently at startup for existing tenants.
    vertex_graphid_indexes: bool = False

    # ── Background jobs (``Prefer: respond-async``) ──────────────────────
    # Jobs each process runs at once.  They are the heavy operations, so the
    # default keeps one at a time; queued jobs wait in the database.
    job_concurrency: int = Field(default=1, ge=1)
    # How often an idle process looks for jobs queued by another replica
    # (a job queued on this process starts at once).
    job_poll_s: float = Field(default=5.0, gt=0)
    # A running job refreshes its heartbeat this often; one silent for three
    # heartbeats is marked failed (its process died).
    job_heartbeat_s: float = Field(default=10.0, gt=0)
    # Finished jobs are deleted after this long.
    job_retention_h: float = Field(default=168.0, gt=0)

    # ── HTTP bind ────────────────────────────────────────────────────────
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    forwarded_allow_ips: str = "127.0.0.1"

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True   # structured JSON to stdout (set false for dev-tty)
    audit_enabled: bool = True
    audit_endpoints_only_writes: bool = True   # log POST/PUT/DELETE/PATCH only

    # ── Rate limiting ────────────────────────────────────────────────────
    # Per-token sliding window. Off by default for tests / dev; on in
    # production via env. Tune `rps` to the typical concurrent-consumer
    # load + ~30% headroom.
    rate_limit_enabled: bool = False
    rate_limit_rps: int = 100
    rate_limit_window_s: float = 1.0

    # ── Revisions ────────────────────────────────────────────────────────
    # When true, every write to one node or edge (PATCH, DELETE, the
    # property merge) must send If-Match with an ETag from a read, or gets
    # 428.  Off by default: If-Match is then honoured when sent.
    require_if_match: bool = False

    # ── Auth ─────────────────────────────────────────────────────────────
    # JSON file: { "<token>": { "consumer": "...", "tenants": ["..."], "admin": false,
    #                           "access": "read" | "write" } }
    auth_tokens_file: Path | None = None
    # Explicit opt-out for local development only.  Without a tokens file
    # graphos refuses to start unless this is true; when it is, every
    # request is treated as an anonymous admin.
    auth_disabled: bool = False

    # NOTE: graphos has no hardcoded tenants. Consumers register their own
    # schemas at runtime via `POST /v1/tenants`. See bootstrap.py and
    # contracts/tenants.py.

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator("auth_tokens_file", mode="before")
    @classmethod
    def _empty_path_to_none(cls, v: Any) -> Any:
        # Treat an empty / whitespace-only env value as ``None`` so an
        # overlay can unset GRAPHOS_AUTH_TOKENS_FILE="" without Pydantic
        # coercing it into ``Path(".")``.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def resolved_db_password(self) -> str:
        """Resolve db_password from inline secret or *_FILE.

        Reads via try/open rather than ``Path.exists()`` because some secret
        mounts (notably Docker Desktop secrets on Windows) raise ``OSError``
        on stat instead of returning a sensible boolean.
        """
        if self.db_password is not None:
            return self.db_password.get_secret_value()
        if self.db_password_file is not None:
            try:
                return self.db_password_file.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(
                    f"GRAPHOS_DB_PASSWORD_FILE='{self.db_password_file}' "
                    f"could not be read: {exc}"
                ) from exc
        raise RuntimeError(
            "GRAPHOS_DB_PASSWORD or GRAPHOS_DB_PASSWORD_FILE must be set"
        )

    def dsn(self) -> str:
        # Percent-encode user and password so characters like @ : / # in a
        # password cannot change how the URL is parsed.
        user = quote(self.db_user, safe="")
        pw = quote(self.resolved_db_password(), safe="")
        return f"postgresql://{user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"
