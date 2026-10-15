"""Fetch the Swagger UI and ReDoc files graphos serves at /docs and /redoc.

graphos serves its API documentation pages from files in the package
(``services/graphos/src/graphos/static/api-docs``), so a browser that cannot
reach a CDN still shows them. This tool puts them there: it downloads the
pinned npm releases from the npm registry, checks each against its published
sha512 integrity before unpacking anything, and copies out the few files the
pages use with their licences. Run it to upgrade (change the pins), then
commit the files and update THIRD_PARTY_NOTICES.md.

    python tools/docs/api_docs_assets.py           # fetch and write
    python tools/docs/api_docs_assets.py --check   # the files match the pins
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "services" / "graphos" / "src" / "graphos" / "static" / "api-docs"
REGISTRY = "https://registry.npmjs.org"

# package -> (version, sha512 integrity, {file in the tarball: name served})
PINS: dict[str, tuple[str, str, dict[str, str]]] = {
    "swagger-ui-dist": (
        "5.33.0",
        "sha512-wpdK+m6BU5yj6pmUdMskZVTSWYG4DLglAx3sIhylloY37i8O37IrH+YEpqdXNfpaTGxILRBFzUqLF2jKqbfI7A==",
        {
            "package/swagger-ui-bundle.js": "swagger-ui-bundle.js",
            "package/swagger-ui.css": "swagger-ui.css",
            "package/favicon-32x32.png": "favicon-32x32.png",
            "package/LICENSE": "LICENSE.swagger-ui",
        },
    ),
    "redoc": (
        "2.5.4",
        "sha512-M6jWhG1qoBnH6TFmzJnstyCZ87HmOY/UzDm78mHiYihEdlV/YcS9ogOo1NlElnJMeLsyxHFe2yFc4sNjHTrABQ==",
        {
            "package/bundles/redoc.standalone.js": "redoc.standalone.js",
            "package/LICENSE": "LICENSE.redoc",
        },
    ),
}
# Kept when the release ships them: Swagger UI's NOTICE (Apache-2.0 asks that
# it travel with the files) and the licences of what redoc.standalone.js bundles.
OPTIONAL = {
    "swagger-ui-dist": {"package/NOTICE": "NOTICE.swagger-ui"},
    "redoc": {"package/bundles/redoc.standalone.js.LICENSE.txt": "redoc.standalone.js.LICENSE.txt"},
}
MANIFEST = "VERSIONS.json"


def _verified(name: str, version: str, integrity: str) -> bytes:
    url = f"{REGISTRY}/{name}/-/{name.split('/')[-1]}-{version}.tgz"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    algorithm, expected = integrity.split("-", 1)
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        sys.exit(f"{name}@{version}: integrity mismatch, nothing written")
    return data


def fetch() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (version, integrity, files) in PINS.items():
        data = _verified(name, version, integrity)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            wanted = {**files, **OPTIONAL.get(name, {})}
            for member in tar.getmembers():
                served = wanted.get(member.name)
                if served is None or not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                assert extracted is not None
                (TARGET / served).write_bytes(extracted.read())
            missing = [f for f in files if not (TARGET / files[f]).exists()]
            if missing:
                sys.exit(f"{name}@{version}: not in the release: {missing}")
        manifest[name] = {"version": version, "integrity": integrity}
        print(f"{name}@{version}: {', '.join(files.values())}")
    (TARGET / MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n",
    )


def check() -> int:
    """0 when the files on disk are those of the pinned releases (by the manifest)."""
    manifest_path = TARGET / MANIFEST
    if not manifest_path.exists():
        print(f"{manifest_path} is missing: run this tool")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stale = [n for n, (v, i, _) in PINS.items() if manifest.get(n) != {"version": v, "integrity": i}]
    absent = [f for _, _, files in PINS.values() for f in files.values() if not (TARGET / f).exists()]
    if stale or absent:
        print(f"out of date: {stale or ''} missing: {absent or ''}")
        return 1
    print("api docs assets: every file matches its pin")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check the files against the pins")
    args = parser.parse_args()
    sys.exit(check() if args.check else fetch())
