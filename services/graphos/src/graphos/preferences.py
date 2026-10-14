"""What a signed-in person chose in the web UI: theme, last tenant, and so on.

One JSON document per subject in ``graphos_meta.user_preferences``, so a new
preference needs no migration: it is one more entry in :data:`KNOWN`.  Only
the names in :data:`KNOWN` are kept, each with the values it allows (a short
list, a boolean, a tenant name), so the document can never carry free text,
personal data or secrets; it is small by construction.  The browser stores
nothing: the UI reads the preferences with ``GET /v1/me`` and changes them
with ``PATCH /v1/me/preferences`` (JSON Merge Patch: ``null`` removes one).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import asyncpg

# A tenant name, as registration accepts it.
_TENANT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


def _one_of(*allowed: str) -> Callable[[Any], bool]:
    return lambda value: value in allowed


def _tenant(value: Any) -> bool:
    return isinstance(value, str) and bool(_TENANT.fullmatch(value))


# Every preference graphos keeps, and the values it accepts.
KNOWN: dict[str, Callable[[Any], bool]] = {
    "theme": _one_of("auto", "light", "dark"),
    "last_tenant": _tenant,
}

DESCRIBED = {
    "theme": "auto, light or dark",
    "last_tenant": "a tenant name",
}


class PreferenceError(ValueError):
    """A change names an unknown preference or gives it a value it does not take."""


def merged(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """``current`` with ``patch`` applied (JSON Merge Patch), checked."""
    out = dict(current)
    for name, value in patch.items():
        check = KNOWN.get(name)
        if check is None:
            known = ", ".join(sorted(KNOWN))
            raise PreferenceError(f"unknown preference {name!r} (known: {known})")
        if value is None:
            out.pop(name, None)
        elif not check(value):
            raise PreferenceError(f"preference {name!r} takes {DESCRIBED[name]}")
        else:
            out[name] = value
    return out


def known_only(stored: Mapping[str, Any]) -> dict[str, Any]:
    """The stored preferences graphos still knows, with values it still takes
    (a preference dropped or narrowed in a later version is left out)."""
    return {k: v for k, v in stored.items() if k in KNOWN and KNOWN[k](v)}


async def load(pool: asyncpg.Pool, subject: str) -> dict[str, Any]:
    text = await pool.fetchval(
        "SELECT prefs::text FROM graphos_meta.user_preferences WHERE subject = $1;", subject,
    )
    return known_only(json.loads(text)) if text else {}


async def update(pool: asyncpg.Pool, subject: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ``patch`` to ``subject``'s preferences; returns them all."""
    async with pool.acquire() as conn, conn.transaction():
        text = await conn.fetchval(
            "SELECT prefs::text FROM graphos_meta.user_preferences WHERE subject = $1 FOR UPDATE;",
            subject,
        )
        prefs = merged(known_only(json.loads(text)) if text else {}, patch)
        await conn.execute(
            """
            INSERT INTO graphos_meta.user_preferences (subject, prefs) VALUES ($1, $2::jsonb)
            ON CONFLICT (subject) DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = now();
            """,
            subject, json.dumps(prefs),
        )
    return prefs

