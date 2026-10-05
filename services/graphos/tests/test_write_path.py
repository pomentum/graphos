"""Every write to graph data goes through graphos.writes.

Scans the SQL and Cypher text in the service's source (string literals,
docstrings excluded) for statements that write rows of a tenant's AGE
label tables.  Any such statement outside ``graphos/writes.py`` fails the
test: revisions and the change log hook in there, and a Cypher write can
crash AGE 1.6 (see the module docstring of ``graphos.writes``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "graphos"

# Writes to tenant label tables are always to quoted identifiers
# ("<tenant>"."<Label>"); graphos's own metadata tables are not quoted.
WRITE_PATTERNS = [
    re.compile(r'\bINSERT\s+INTO\s+"', re.I),
    re.compile(r'\bUPDATE\s+"', re.I),
    re.compile(r'\bDELETE\s+FROM\s+"', re.I),
    re.compile(r"\bcopy_records_to_table\b"),
    # Cypher writes: CREATE / MERGE of a node or path pattern ...
    re.compile(r"\b(CREATE|MERGE)\s*\(\s*\w*\s*[:{)]", re.I),
    # ... and MATCH ... SET n += / SET n.key = / DETACH DELETE.
    re.compile(r"\bMATCH\b.*\b(SET\s+\w+(\s*\+=|\.\w+\s*=)|DETACH\s+DELETE)", re.I | re.S),
]

# Generates the resolve INSERT as text; graphos.writes.insert_resolved_edges
# runs it.
ALLOWED = {"writes.py", "resolver/compiler.py"}


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal except docstrings, f-strings as one text (each
    interpolation shown as ``{}``), plus a marker for each COPY call."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        elif isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
            out.append((node.lineno, text))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                out.append((node.lineno, node.value))
        elif isinstance(node, ast.Attribute) and node.attr == "copy_records_to_table":
            out.append((node.lineno, "copy_records_to_table"))
    return out


def test_graph_writes_only_in_writes_module() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, text in _strings(tree):
            for pat in WRITE_PATTERNS:
                if pat.search(text):
                    offenders.append(f"{rel}:{lineno}: {text.strip()[:80]!r}")
    assert not offenders, "graph writes outside graphos/writes.py:\n" + "\n".join(offenders)


def test_guard_catches_a_write() -> None:
    for code in (
        'sql = f\'UPDATE "{t}"."X" SET properties = 1\'',
        'conn.copy_records_to_table("s")',
        'q = "MATCH (n {id: $id}) DETACH DELETE n"',
        'q = "MATCH (n) SET n.k = 1"',
        "q = f\"MATCH (n) WHERE n.{key} = $v \" \"DETACH DELETE n\"",
        'q = "CREATE (:Doc {id: 1})"',
    ):
        texts = [t for _, t in _strings(ast.parse(code))]
        assert any(p.search(t) for t in texts for p in WRITE_PATTERNS), code


def test_guard_ignores_prose_and_metadata() -> None:
    for code in (
        'd = "Properties merge additively (like openCypher `SET n += map`)."',
        'r = r"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS"',
        'q = "INSERT INTO graphos_meta.cache (tenant) VALUES ($1)"',
        'q = "SET LOCAL statement_timeout = 1000"',
    ):
        texts = [t for _, t in _strings(ast.parse(code))]
        assert not any(p.search(t) for t in texts for p in WRITE_PATTERNS), code
