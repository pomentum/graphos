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

    # ── Graph metrics (``graphos.graph_metrics``) ─────────────────────────
    # Exact betweenness costs O(nodes x edges): above this many nodes taking
    # part, a metrics run skips it (and says so).
    betweenness_max_nodes: int = 20_000

    # ── Rate limiting ────────────────────────────────────────────────────
    # Per-token sliding window. Off by default for tests / dev; on in
    # production via env. Tune `rps` to the typical concurrent-consumer
    # load + ~30% headroom.
    rate_limit_enabled: bool = False
    rate_limit_rps: int = 100
    rate_limit_window_s: float = 1.0

    # ── Several replicas (``graphos.sync``) ───────────────────────────────
    # Each replica re-reads every tenant registration this often, in case a
    # change notification was missed (they are normally applied at once).
    registry_sync_s: float = Field(default=30.0, gt=0)
    # Longest a K/V cache entry is served from this replica's memory without
    # re-reading the table (bounds staleness if a change was not announced).
    cache_max_age_s: float = Field(default=30.0, gt=0)

    # Seconds a stopping server waits for open requests (change-feed streams
    # never finish on their own) before cancelling them.
    shutdown_grace_s: float = Field(default=5.0, gt=0)

    # ── Change feed ──────────────────────────────────────────────────────
    # Records older than this are pruned; a cursor behind them gets 410.
    changes_retention_h: float = Field(default=168.0, gt=0)
    # Live streams look for new records at least this often, even without a
    # notification (which normally wakes them at once).
    changes_poll_s: float = Field(default=1.0, gt=0)
    # A comment line sent on an idle stream so proxies keep it open.
    changes_heartbeat_s: float = Field(default=15.0, gt=0)

    # ── Revisions ────────────────────────────────────────────────────────
    # When true, every write to one node or edge (PATCH, DELETE, the
    # property merge) must send If-Match with an ETag from a read, or gets
    # 428.  Off by default: If-Match is then honoured when sent.
    require_if_match: bool = False

    # ── Auth (graphos.identity) ──────────────────────────────────────────
    # Explicit opt-out for local development only: every request is an
    # anonymous admin.
    auth_disabled: bool = False
    # The bootstrap admin's first password, read once when no user exists;
    # without it, user "graphos" gets the password "graphos".  Either way it
    # must be changed at the first sign-in.
    admin_password_file: Path | None = None
    # False: the bootstrap admin "graphos" cannot sign in (once other admins exist).
    local_admin: bool = True
    # ``iss`` of the tokens graphos signs; set it per deployment when several
    # graphos instances share users of one identity provider.
    auth_issuer: str = "graphos"
    # A PEM EC P-256 private key to sign with, instead of the key graphos
    # generates and keeps in graphos_meta.signing_keys.
    auth_signing_key_file: Path | None = None
    access_token_ttl_s: int = Field(default=900, ge=60, le=86_400)
    # The web UI's session cookie (the refresh token, HttpOnly, SameSite=Strict)
    # is Secure: sent over HTTPS only (browsers also accept it on http://localhost).
    # False only for a plain-HTTP deployment on another host.
    auth_cookie_secure: bool = True
    # Browser sessions (refresh tokens, people only): idle and absolute limits.
    session_idle_s: int = Field(default=3_600, ge=60)
    session_max_s: int = Field(default=43_200, ge=300)
    # Sign-in protection: failures before a lock, its length, and attempts
    # per minute from one client address.
    login_max_failures: int = Field(default=5, ge=1)
    login_lockout_s: int = Field(default=900, ge=1)
    login_attempts_per_min: int = Field(default=20, ge=1)
    password_min_length: int = Field(default=12, ge=8)

    # ── Single sign-on (graphos.oidc) ────────────────────────────────────
    # Any OpenID Connect provider, found through its discovery document
    # (<issuer>/.well-known/openid-configuration).  Unset: no SSO.
    oidc_issuer: str | None = None
    # graphos's one registration at the provider: a public client using PKCE.
    oidc_client_id: str | None = None
    # The ``aud`` access tokens sent straight to the API must carry; defaults
    # to the client id.
    oidc_audience: str | None = None
    oidc_scopes: str = "openid profile email"
    # Shown on the sign-in button: "Sign in with <name>" ("Sign in with SSO";
    # or the provider's name).
    oidc_display_name: str = "SSO"
    # Claims: the user name shown and recorded, group and role lists.
    oidc_consumer_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_roles_claim: str = "roles"
    # Redirect URIs the UI may ask for (the provider checks its own list as
    # well); empty: any http(s) URI.
    oidc_redirect_uris: list[str] = Field(default_factory=list)
    oidc_http_timeout_s: float = Field(default=10.0, gt=0)

    # NOTE: graphos has no hardcoded tenants. Consumers register their own
    # schemas at runtime via `POST /v1/tenants`. See bootstrap.py and
    # contracts/tenants.py.

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator(
        "admin_password_file", "auth_signing_key_file",
        "oidc_issuer", "oidc_client_id", "oidc_audience", mode="before",
    )
    @classmethod
    def _empty_path_to_none(cls, v: Any) -> Any:
        # Treat an empty / whitespace-only env value as ``None`` so an
        # overlay can unset a file setting ("") without Pydantic coercing it
        # into ``Path(".")``.
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
