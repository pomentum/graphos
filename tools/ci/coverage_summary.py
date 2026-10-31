#!/usr/bin/env python3
"""coverage_summary.py — a coverage report as Markdown, for a CI job summary.

    python3 tools/ci/coverage_summary.py --title "Service" coverage.xml
    python3 tools/ci/coverage_summary.py --title "Web UI" coverage/coverage-summary.json

Reads a Cobertura XML report (pytest-cov's ``--cov-report=xml``) or an Istanbul
``json-summary`` (Vitest's), and prints the total and the least-covered files.
GitHub renders what a job appends to ``$GITHUB_STEP_SUMMARY`` on the run's page.
Standard library only, so it runs on any runner's Python.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LEAST = 15


def _cobertura(path: Path) -> tuple[float, list[tuple[str, int, int]]]:
    """The total line coverage and, per file, (name, lines, lines missed)."""
    root = ET.parse(path).getroot()
    files: dict[str, list[int]] = {}
    for cls in root.iter("class"):
        lines = cls.findall("lines/line")
        hit = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        entry = files.setdefault(cls.get("filename", "?"), [0, 0])
        entry[0] += len(lines)
        entry[1] += len(lines) - hit
    total = float(root.get("line-rate", "0")) * 100
    return total, [(name, n, missed) for name, (n, missed) in files.items()]


def _istanbul(path: Path) -> tuple[float, list[tuple[str, int, int]]]:
    """The same from an Istanbul json-summary (statements)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    total = float(data["total"]["statements"]["pct"])
    rows = []
    for name, entry in data.items():
        if name == "total":
            continue
        s = entry["statements"]
        rows.append((name, int(s["total"]), int(s["total"]) - int(s["covered"])))
    return total, rows


def _short(name: str) -> str:
    """A file name without the runner's checkout path."""
    for marker in ("/src/", "\\src\\"):
        if marker in name:
            return "src/" + name.split(marker, 1)[1].replace("\\", "/")
    return name.replace("\\", "/")


def summary(title: str, path: Path) -> str:
    total, rows = _istanbul(path) if path.suffix == ".json" else _cobertura(path)
    measured = [r for r in rows if r[1] > 0]
    lines = sum(r[1] for r in measured)
    missed = sum(r[2] for r in measured)
    least = sorted(measured, key=lambda r: ((r[1] - r[2]) / r[1], -r[1]))[:LEAST]
    out = [
        f"### Coverage: {title} — {total:.1f}%",
        "",
        f"{len(measured)} files, {lines:,} lines, {missed:,} not run by the tests.",
        "",
        f"<details><summary>The {len(least)} least-covered files</summary>",
        "",
        "| File | Lines | Not run | Covered |",
        "|---|---:|---:|---:|",
    ]
    out += [
        f"| `{_short(name)}` | {n:,} | {m:,} | {100 * (n - m) / n:.0f}% |" for name, n, m in least
    ]
    out += ["", "</details>", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="Cobertura XML or Istanbul json-summary")
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")   # the job summary is UTF-8 on any runner
    if not args.report.is_file():
        print(f"### Coverage: {args.title} — no report ({args.report} missing)\n")
        return 0
    print(summary(args.title, args.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
