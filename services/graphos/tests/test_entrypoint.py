"""The image's entrypoint (docker-entrypoint.sh): python-igraph when asked for, then graphos.

Runs the script under ``sh`` with stand-ins for ``python`` and ``pip`` on the
``PATH``, which record how they were called; nothing is installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
SCRIPT = SERVICE / "docker-entrypoint.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

# ``python -c "import igraph"`` succeeds when $FAKE_IGRAPH is set or the extras
# folder holds the install; ``python -m graphos ...`` records its PYTHONPATH.
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-c" ]; then
    [ -n "${FAKE_IGRAPH:-}" ] && exit 0
    exit 1
fi
echo "python $* PYTHONPATH=${PYTHONPATH:-}" >> "$CALLS"
"""
FAKE_PIP = """#!/bin/sh
echo "pip $*" >> "$CALLS"
[ -n "${FAKE_PIP_FAILS:-}" ] && exit 1
while [ $# -gt 0 ]; do
    [ "$1" = "--target" ] && mkdir -p "$2"
    shift
done
exit 0
"""


def _run(tmp_path: Path, *args: str, **env: str) -> tuple[list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("python", FAKE_PYTHON), ("pip", FAKE_PIP)):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls.txt"
    calls.unlink(missing_ok=True)
    script = SCRIPT.read_text(encoding="utf-8").replace(
        "extras=/tmp/graphos-extras", f"extras={(tmp_path / 'extras').as_posix()}")
    run = tmp_path / "entrypoint.sh"
    run.write_text(script, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [SH, run.as_posix(), *args], capture_output=True, text=True, check=True,
        env={"PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
             "CALLS": calls.as_posix(), "PYTHONPATH": "/app", **env},
    )
    return calls.read_text(encoding="utf-8").splitlines(), done.stderr


def test_by_default_nothing_is_installed_and_graphos_starts(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path)
    assert calls == ["python -m graphos PYTHONPATH=/app"]


def test_igraph_is_installed_once_and_put_on_the_path(tmp_path: Path) -> None:
    calls, err = _run(tmp_path, GRAPHOS_IGRAPH="true")
    assert calls[0].startswith("pip install ") and calls[0].endswith(" python-igraph>=0.11,<1.0")
    assert "--target" in calls[0]
    assert calls[1] == f"python -m graphos PYTHONPATH=/app:{(tmp_path / 'extras').as_posix()}"
    assert "installing python-igraph" in err
    (tmp_path / "extras").mkdir(exist_ok=True)
    (tmp_path / "extras" / ".installed").write_text("python-igraph>=0.11,<1.0\n")
    calls, _ = _run(tmp_path, GRAPHOS_IGRAPH="true")      # a restart: kept
    assert [c for c in calls if c.startswith("pip")] == []


def test_nothing_is_installed_when_igraph_is_in_the_image(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, GRAPHOS_IGRAPH="true", FAKE_IGRAPH="1")
    assert calls == ["python -m graphos PYTHONPATH=/app"]


def test_a_requirement_turns_it_on_as_the_old_overlay_set_it(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, GRAPHOS_IGRAPH="python-igraph==0.11.8")
    assert calls[0].endswith(" python-igraph==0.11.8")


def test_a_failed_install_still_starts_graphos(tmp_path: Path) -> None:
    calls, err = _run(tmp_path, GRAPHOS_IGRAPH="true", FAKE_PIP_FAILS="1")
    assert calls[-1] == "python -m graphos PYTHONPATH=/app"
    assert "starting without Louvain and Leiden" in err
    assert not (tmp_path / "extras").exists()


def test_arguments_go_to_graphos(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, "reset-admin-password")
    assert calls == ["python -m graphos reset-admin-password PYTHONPATH=/app"]


def test_the_default_requirement_is_the_louvain_extra() -> None:
    extra = tomllib.loads((SERVICE / "pyproject.toml").read_text(encoding="utf-8"))
    louvain = extra["project"]["optional-dependencies"]["louvain"]
    default = re.search(r'GRAPHOS_IGRAPH_PACKAGE:-([^}"]+)', SCRIPT.read_text(encoding="utf-8"))
    assert default is not None and louvain == [default[1]]
