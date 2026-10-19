"""Which databases graphos refuses to start on (no DB; ADR-0007)."""

from __future__ import annotations

import pytest

from graphos.db import unsupported_database


@pytest.mark.parametrize(
    ("postgres", "age", "refused"),
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
def test_postgresql_before_18_and_age_before_1_8_are_refused(
    postgres: str, age: str | None, refused: bool,  # noqa: FBT001
) -> None:
    reason = unsupported_database(postgres, age)
    assert (reason is not None) is refused
    if reason:
        assert "not supported" in reason and "upgrade-postgresql-18.md" in reason


@pytest.mark.parametrize(
    ("postgres", "age", "refused"),
    [
        ("18beta1", "1.8rc1", False),
        ("18devel", "1.8-rc1", False),
        ("18.1 (Debian 18.1-1.pgdg13+1)", "1.8.0-rc1", False),
        ("18.6", "1.10.0", False),
        ("garbage", "1.8.0", True),
    ],
)
def test_prerelease_and_unusual_versions_are_read_by_their_leading_numbers(
    postgres: str, age: str | None, refused: bool,  # noqa: FBT001
) -> None:
    assert (unsupported_database(postgres, age) is not None) is refused
