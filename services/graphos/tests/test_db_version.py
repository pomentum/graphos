"""Which databases graphos calls deprecated (no DB)."""

from __future__ import annotations

import pytest

from graphos.db import deprecated_database


@pytest.mark.parametrize(
    ("postgres", "age", "deprecated"),
    [
        ("18.6 (Debian 18.6-1.pgdg13+2)", "1.8.0", False),
        ("18.1", "1.9.0", False),
        ("19.0", "1.8.0", False),
        ("16.10 (Debian 16.10-1.pgdg13+1)", "1.6.0", True),
        ("17.4", "1.8.0", True),
        ("18.6", "1.7.0", True),
        ("18.6", None, True),
    ],
)
def test_postgresql_before_18_and_age_before_18_are_deprecated(
    postgres: str, age: str | None, deprecated: bool,  # noqa: FBT001
) -> None:
    reason = deprecated_database(postgres, age)
    assert (reason is not None) is deprecated
    if reason:
        assert "deprecated" in reason and "upgrade-postgresql-18.md" in reason
