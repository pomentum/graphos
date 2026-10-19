"""Secrets graphos keeps in its database, sealed with a master key it does not.

graphos stores two secrets of its own in ``graphos_meta``: the private keys
it signs access tokens with (``signing_keys.private_pem``) and the identity
provider's ID token of each signed-in session (``sessions.id_token``, sent
back to the provider at sign-out).  In clear, every database dump and every
backup carries them; with the signing key, whoever holds a dump can mint
tokens for any user.  So both are sealed here before they are written.

**The scheme.**  Each value gets a random 16-byte salt and 12-byte nonce.
The key is HKDF-SHA256 over the master key and the salt, so no two rows
share a key; the value is encrypted with AES-256-GCM, with the row's
identity (``signing_key:<kid>``, ``session:<family>``) as associated data,
so a sealed value copied onto another row does not open there.  A sealed
value reads ``v1:<key version>:<base64 of salt | nonce | ciphertext>``.

**Master keys** come from ``GRAPHOS_MASTER_KEY_FILE``, never from the
database: one key per line, ``<version>:<base64 of 32 bytes>``.  The
highest version seals; every version listed opens, so a new key is added
as a new line, everything is re-sealed (``python -m graphos.vault reseal``)
and the old line removed.  Losing the file means losing what it sealed:
graphos refuses to start on sealed signing keys it cannot open.  To
recover, delete the rows of ``graphos_meta.signing_keys`` (graphos makes a
new key; everyone signs in again) and null ``id_token`` in
``graphos_meta.sessions`` (docs/installation.md §8).

**Without a master key** values are stored as they are, as before, with a
warning at start.  A value that is not sealed is read as it is, so rows
written before a master key existed keep working; graphos seals them as
they are written again, and the signing keys at start.

Not pgcrypto: the key would travel to the database with every query and
show up in its logs and ``pg_stat_statements``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

_PREFIX = "v1:"
_SALT, _NONCE = 16, 12
_INFO = b"graphos-vault-v1"
_LINE = re.compile(r"^(\d{1,6}):([A-Za-z0-9+/=_-]+)$")


class VaultError(Exception):
    """A sealed value cannot be opened, or the master key file is unusable."""


@dataclass(frozen=True)
class Vault:
    """The master keys, by version; empty when graphos runs without one."""

    keys: Mapping[int, bytes]

    @property
    def enabled(self) -> bool:
        return bool(self.keys)

    @classmethod
    def from_file(cls, path: Path) -> Vault:
        """Read ``<version>:<base64>`` lines; blank lines and ``#`` comments are skipped."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VaultError(f"cannot read GRAPHOS_MASTER_KEY_FILE {path}: {exc}") from None
        keys: dict[int, bytes] = {}
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE.fullmatch(line)
            key = _decode(m[2]) if m else None
            if m is None or key is None or len(key) != 32:  # noqa: PLR2004 — AES-256
                raise VaultError(
                    f"{path}, line {number}: not '<version>:<base64 of 32 bytes>'",
                )
            keys[int(m[1])] = key
        if not keys:
            raise VaultError(f"GRAPHOS_MASTER_KEY_FILE {path} holds no key")
        return cls(keys)

    @staticmethod
    def is_sealed(value: str) -> bool:
        return value.startswith(_PREFIX)

    def seal(self, plain: str, context: str) -> str:
        """``plain`` sealed for the row named ``context``; as it is without a master key."""
        if not self.keys:
            return plain
        version = max(self.keys)
        salt, nonce = os.urandom(_SALT), os.urandom(_NONCE)
        sealed = AESGCM(self._key(version, salt)).encrypt(
            nonce, plain.encode(), context.encode(),
        )
        blob = base64.b64encode(salt + nonce + sealed).decode()
        return f"{_PREFIX}{version}:{blob}"

    def open(self, value: str, context: str) -> str:
        """The plain value; a value that is not sealed comes back as it is.

        Raises:
            VaultError: sealed with a key this vault does not hold, or for
                another row, or altered.
        """
        if not self.is_sealed(value):
            return value
        try:
            version_text, blob = value[len(_PREFIX):].split(":", 1)
            version = int(version_text)
            data = base64.b64decode(blob, validate=True)
        except ValueError:
            raise VaultError("not a sealed value") from None
        if version not in self.keys:
            raise VaultError(
                f"sealed with master key version {version}, which GRAPHOS_MASTER_KEY_FILE "
                "does not hold",
            )
        salt, nonce, sealed = data[:_SALT], data[_SALT:_SALT + _NONCE], data[_SALT + _NONCE:]
        try:
            plain = AESGCM(self._key(version, salt)).decrypt(nonce, sealed, context.encode())
        except Exception:  # noqa: BLE001 — cryptography raises InvalidTag, nothing to add
            raise VaultError("a sealed value does not open: altered, or of another row") from None
        return plain.decode()

    def reseal(self, value: str, context: str) -> str:
        """``value`` sealed with the newest key, whatever it was sealed with (or not)."""
        return self.seal(self.open(value, context), context)

    def _key(self, version: int, salt: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_INFO).derive(
            self.keys[version],
        )


def _decode(text: str) -> bytes | None:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
    except ValueError:
        return None


def new_key_line(version: int = 1) -> str:
    """A fresh master key line for GRAPHOS_MASTER_KEY_FILE."""
    return f"{version}:{base64.b64encode(os.urandom(32)).decode()}"


# The vault in use; set at start from GRAPHOS_MASTER_KEY_FILE (graphos.app).
_vault = Vault({})


def configure(path: Path | None) -> Vault:
    """Load the master keys (``None``: no master key, values stored in clear)."""
    global _vault  # noqa: PLW0603 — one vault per process, set once at start
    _vault = Vault.from_file(path) if path is not None else Vault({})
    if not _vault.enabled:
        log.warning(
            "no GRAPHOS_MASTER_KEY_FILE: signing keys and ID tokens are stored in clear; "
            "run scripts/init-secrets.sh and set it",
        )
    return _vault


def current() -> Vault:
    return _vault


def signing_key_context(kid: str) -> str:
    return f"signing_key:{kid}"


def session_context(family: object) -> str:
    return f"session:{family}"


# ── Re-sealing (after adding a master key) ──────────────────────────────────


async def reseal_all(conn: asyncpg.Connection, vault: Vault) -> tuple[int, int]:
    """Seal every signing key and ID token with the newest master key, in one
    transaction.  Returns how many keys and tokens were written."""
    if not vault.enabled:
        raise VaultError("GRAPHOS_MASTER_KEY_FILE is not set: nothing to seal with")
    keys = tokens = 0
    async with conn.transaction():
        for row in await conn.fetch(
            "SELECT kid, private_pem FROM graphos_meta.signing_keys FOR UPDATE;",
        ):
            ctx = signing_key_context(row["kid"])
            await conn.execute(
                "UPDATE graphos_meta.signing_keys SET private_pem = $2 WHERE kid = $1;",
                row["kid"], vault.reseal(row["private_pem"], ctx),
            )
            keys += 1
        for row in await conn.fetch(
            "SELECT id, family, id_token FROM graphos_meta.sessions "
            "WHERE id_token IS NOT NULL FOR UPDATE;",
        ):
            ctx = session_context(row["family"])
            await conn.execute(
                "UPDATE graphos_meta.sessions SET id_token = $2 WHERE id = $1;",
                row["id"], vault.reseal(row["id_token"], ctx),
            )
            tokens += 1
    return keys, tokens


async def _reseal_command() -> int:
    from graphos.config import Settings  # noqa: PLC0415 — the command only

    settings = Settings()
    vault = configure(settings.master_key_file)
    conn = await asyncpg.connect(settings.dsn())
    try:
        keys, tokens = await reseal_all(conn, vault)
    finally:
        await conn.close()
    print(f"sealed {keys} signing keys and {tokens} ID tokens with master key "  # noqa: T201
          f"version {max(vault.keys)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m graphos.vault",
        description="Seal graphos's stored secrets with the master key.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reseal", help="seal every signing key and ID token with the newest key")
    new = sub.add_parser("new-key", help="print a new master key line")
    new.add_argument("--version", type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == "new-key":
        print(new_key_line(args.version))  # noqa: T201
        return 0
    try:
        return asyncio.run(_reseal_command())
    except VaultError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201
        return 1


if __name__ == "__main__":
    sys.exit(main())
