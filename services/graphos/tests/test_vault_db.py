"""DB tests: signing keys and ID tokens are sealed where they are stored.

DB-gated.  The identity tables are instance-wide: each test starts with no
signing key and no session, as a first start would, and puts the process's
vault back to none at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from graphos import identity, vault
from graphos.vault import VaultError

from .conftest import requires_db


@pytest_asyncio.fixture
async def clean(db_pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Pool]:
    wipe = "DELETE FROM graphos_meta.sessions; DELETE FROM graphos_meta.signing_keys;"
    await db_pool.execute(wipe)
    yield db_pool
    await db_pool.execute(wipe)
    vault.configure(None)


def _key_file(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "master_key.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _stored_pems(pool: asyncpg.Pool) -> list[str]:
    return [r["private_pem"] for r in await pool.fetch(
        "SELECT private_pem FROM graphos_meta.signing_keys;",
    )]


@requires_db
@pytest.mark.asyncio
async def test_a_new_signing_key_is_stored_sealed_and_still_signs(
    clean: asyncpg.Pool, tmp_path: Path,
) -> None:
    vault.configure(_key_file(tmp_path, vault.new_key_line()))
    signer = identity.Signer(issuer="graphos", ttl_s=60)
    await signer.load(clean)
    (stored,) = await _stored_pems(clean)
    assert stored.startswith("v1:1:") and "PRIVATE KEY" not in stored
    # A second replica opens the same key: tokens one signs, the other verifies.
    other = identity.Signer(issuer="graphos", ttl_s=60)
    await other.load(clean)
    token, _ = signer._sign({"sub": "someone"})
    assert other.verify(token)["sub"] == "someone"


@requires_db
@pytest.mark.asyncio
async def test_a_key_stored_in_clear_is_sealed_at_start_once_a_master_key_is_set(
    clean: asyncpg.Pool, tmp_path: Path,
) -> None:
    vault.configure(None)
    await identity.Signer(issuer="graphos", ttl_s=60).load(clean)
    (plain,) = await _stored_pems(clean)
    assert "PRIVATE KEY" in plain
    vault.configure(_key_file(tmp_path, vault.new_key_line()))
    await identity.Signer(issuer="graphos", ttl_s=60).load(clean)
    (sealed,) = await _stored_pems(clean)
    assert vault.Vault.is_sealed(sealed)
    # Without the master key, a sealed key cannot be used: graphos refuses to start.
    vault.configure(None)
    with pytest.raises(VaultError, match="version 1"):
        await identity.Signer(issuer="graphos", ttl_s=60).load(clean)


@requires_db
@pytest.mark.asyncio
async def test_a_sessions_id_token_is_sealed_and_comes_back_at_sign_out(
    clean: asyncpg.Pool, tmp_path: Path,
) -> None:
    vault.configure(_key_file(tmp_path, vault.new_key_line()))
    refresh = (await identity.open_session(
        clean, "someone", idle_s=60, max_s=600, id_token="eyJ.provider.token",
    )).token
    stored = await clean.fetchval("SELECT id_token FROM graphos_meta.sessions;")
    assert vault.Vault.is_sealed(stored) and "provider" not in stored
    renewed = await identity.rotate_session(clean, refresh, idle_s=60)
    assert renewed.refresh_token is not None
    ended = await identity.close_session(clean, renewed.refresh_token)
    assert ended is not None and ended.id_token == "eyJ.provider.token"


@requires_db
@pytest.mark.asyncio
async def test_reseal_moves_everything_to_the_newest_master_key(
    clean: asyncpg.Pool, tmp_path: Path,
) -> None:
    first = vault.new_key_line(1)
    vault.configure(_key_file(tmp_path, first))
    await identity.Signer(issuer="graphos", ttl_s=60).load(clean)
    await identity.open_session(clean, "someone", idle_s=60, max_s=600, id_token="t")
    second = vault.new_key_line(2)
    rotated = vault.configure(_key_file(tmp_path, first, second))
    async with clean.acquire() as conn:
        assert await vault.reseal_all(conn, rotated) == (1, 1)
    stored = [*await _stored_pems(clean), await clean.fetchval(
        "SELECT id_token FROM graphos_meta.sessions;",
    )]
    assert all(s.startswith("v1:2:") for s in stored)
    # With the old line removed, everything still opens; with another key, nothing does.
    vault.configure(_key_file(tmp_path, second))
    await identity.Signer(issuer="graphos", ttl_s=60).load(clean)
    vault.configure(_key_file(tmp_path, vault.new_key_line(3)))
    with pytest.raises(VaultError):
        await identity.Signer(issuer="graphos", ttl_s=60).load(clean)
