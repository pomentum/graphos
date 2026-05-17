"""The DDL validator must lex literals exactly as Postgres does.

If the validator and Postgres disagree about where a string literal or a quoted
identifier ends, text the validator thinks is inside a literal can be a second
ALTER TABLE subcommand to Postgres.  Each case below was a working bypass or a
close variant of one.
"""

from __future__ import annotations

import pytest

from graphos.contracts.ddl import validate_ddl

_TABLES = ["Doc"]
_TAIL = "STORED, DROP COLUMN properties, ADD COLUMN d text GENERATED ALWAYS AS (1) STORED"


@pytest.mark.parametrize(
    "stmt",
    [
        # E'' strings: \' is an escape to Postgres, the end of a literal to a naive lexer.
        f"ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (E'\\'') {_TAIL}",
        f"ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (e'x\\'y') {_TAIL}",
        # A quoted identifier containing an apostrophe.
        f"ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (\"a'b\") {_TAIL}",
        # Unicode-escape literals and identifiers.
        f"ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (U&'\\0027') {_TAIL}",
        f"ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (U&\"x\") {_TAIL}",
        # Bit / hex string prefixes.
        "ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (B'1') STORED",
        "ALTER TABLE acme.\"Doc\" ADD COLUMN c text GENERATED ALWAYS AS (X'1F') STORED",
        # Backslash anywhere; non-ASCII anywhere.
        "CREATE INDEX IF NOT EXISTS i ON acme.\"Doc\" ((properties::text ~ '\\d'))",
        "CREATE INDEX IF NOT EXISTS i ON acme.\"Doc\" ((properties::text = 'é'))",
    ],
)
def test_ambiguous_lexing_is_rejected(stmt: str) -> None:
    kind = "index" if stmt.startswith("CREATE") else "generated_column"
    with pytest.raises(ValueError):
        validate_ddl(stmt, kind=kind, tenant="acme", tables=_TABLES)


def test_quoted_identifiers_and_plain_literals_still_work() -> None:
    stmt = (
        'ALTER TABLE "acme"."Doc" ADD COLUMN IF NOT EXISTS "Region" text '
        "GENERATED ALWAYS AS (((properties::text)::jsonb ->> 'it''s')) STORED"
    )
    assert validate_ddl(stmt, kind="generated_column", tenant="acme", tables=_TABLES) == stmt


@pytest.mark.parametrize(
    "stmt",
    [
        # Column constraints: CHECK expressions need not be immutable, so they
        # could run arbitrary functions on every write.
        'ALTER TABLE "acme"."Doc" ADD COLUMN c int CHECK (pg_sleep(5) IS NOT NULL) '
        "GENERATED ALWAYS AS (1) STORED",
        'ALTER TABLE "acme"."Doc" ADD COLUMN c int DEFAULT 1 GENERATED ALWAYS AS (1) STORED',
        'ALTER TABLE "acme"."Doc" ADD COLUMN c int NOT NULL GENERATED ALWAYS AS (1) STORED',
        'ALTER TABLE "acme"."Doc" ADD COLUMN c int COLLATE "C" GENERATED ALWAYS AS (1) STORED',
    ],
)
def test_generated_column_admits_only_a_type(stmt: str) -> None:
    with pytest.raises(ValueError):
        validate_ddl(stmt, kind="generated_column", tenant="acme", tables=_TABLES)


def test_unquoted_identifiers_fold_to_lower_case() -> None:
    # Postgres folds Acme to acme: for tenant "Acme" that is someone else's schema.
    with pytest.raises(ValueError):
        validate_ddl(
            "CREATE INDEX IF NOT EXISTS i ON Acme._ag_label_vertex (id)",
            kind="index", tenant="Acme", tables=["Doc"],
        )
    # Unquoted works when it folds to the real name; quoted matches exactly.
    validate_ddl(
        "CREATE INDEX IF NOT EXISTS i ON acme._ag_label_vertex (id)",
        kind="index", tenant="acme", tables=["Doc"],
    )
    validate_ddl(
        'CREATE INDEX IF NOT EXISTS i ON "Acme"."Doc" (id)',
        kind="index", tenant="Acme", tables=["Doc"],
    )
