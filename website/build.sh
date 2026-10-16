#!/usr/bin/env bash
# Build the website into website/.build/site, as the Pages workflow does.
# Usage: bash website/build.sh [python]   (default: python3)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${1:-python3}"
"$PY" website/assemble.py
"$PY" -m mkdocs build --strict -f website/mkdocs.yml
