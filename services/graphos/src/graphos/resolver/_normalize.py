"""Single source of truth for the ``eq_norm`` canonical-fold SQL expression.

The resolver join (``compiler.py``) and the supporting functional index
(``indexer.py``) MUST wrap their operand in the byte-identical SQL
expression, or Postgres won't use the index for the join — and worse,
the two sides could fold differently and silently stop matching.  Defining
the fold in exactly one place and importing it into both keeps them in
lockstep by construction.

Domain-agnostic by design.  This is a *surface canonical fold* only:
collapse internal whitespace, lower-case, and trim.  It does not strip
punctuation, interpret the token's structure, or make any validity
judgement (it never rejects or rewrites a value based on its shape).
Deciding which values are meaningful to match is the writer's concern and
belongs in the data it loads, not here.  This keeps ``eq_norm`` exactly as
agnostic as ``eq_ci``'s ``lower(...)``.

The expression is composed only of IMMUTABLE builtins (``lower``,
``btrim``, ``regexp_replace``) so it is legal inside a functional index.

Maps onto the standard entity-resolution pipeline: this is the
*preprocessing / canonicalization* stage that produces the blocking key
the functional index materialises and the join compares.
"""

from __future__ import annotations


def canon_text_sql(inner_sql: str) -> str:
    """Wrap a text-typed SQL expression in the canonical fold.

    ``inner_sql`` must be a SQL fragment that evaluates to ``text`` (e.g.
    ``(props ->> 'name')``, ``$1::text``, or a ``||``-concatenation).  The
    returned fragment is what BOTH the compiler join predicate and the
    indexer functional-index DDL must use, verbatim, for ``eq_norm``.

    Fold steps (all IMMUTABLE):
      1. ``regexp_replace(x, '\\s+', ' ', 'g')`` — collapse any run of
         whitespace (spaces, tabs, newlines) to a single space.
      2. ``lower(...)``                          — case fold.
      3. ``btrim(...)``                          — strip leading/trailing
         whitespace left after the collapse.
    """
    return f"btrim(lower(regexp_replace({inner_sql}, '\\s+', ' ', 'g')))"


__all__ = ["canon_text_sql"]
