#!/usr/bin/env bash
# Build the website into website/.build/site, as the Pages workflow does.
# Usage: bash website/build.sh [python]   (default: python3; standard library only)
set -euo pipefail
cd "$(dirname "$0")/.."
"${1:-python3}" website/build.py
