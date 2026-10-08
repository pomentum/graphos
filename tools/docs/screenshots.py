"""Screenshots of the web UI for the docs (docs/images/ui-*.png, and .jpg).

Takes them from a running UI, signed in as a user that sees only the demo
tenant (so no other tenant's name appears), in dark mode, at 1440x900.  Load
the demo first: ``clients/python/examples/08_demo_ecosystem.py``.  Each shot is
written twice: a PNG for the README and docs, and a smaller JPEG that
``node tools/docs/diagrams.mjs`` embeds in the whitepaper (one self-contained file).

    pip install playwright            # plus a browser: playwright install chromium,
                                      # or --channel msedge / chrome to use an installed one
    export GRAPHOS_USER=... GRAPHOS_PASSWORD=...   # a person, granted only the demo tenant
    python tools/docs/screenshots.py --ui http://localhost:5173

Only the shots named on the command line are taken when any are given:
``python tools/docs/screenshots.py explore-graph schema-diagram``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SETTLE_MS = 3500          # layouts animate: let them come to rest
JPEG_QUALITY = 80         # the whitepaper's copies: a quarter of the PNG's size


def _go(page: Page, path: str) -> None:
    """Navigate inside the app (a reload would sign out: nothing is stored)."""
    page.evaluate(
        "p => { history.pushState({}, '', p); dispatchEvent(new PopStateEvent('popstate')); }",
        path,
    )
    page.wait_for_timeout(SETTLE_MS)


def _mode(page: Page, name: str) -> None:
    page.get_by_text(name, exact=True).first.click()
    page.wait_for_timeout(SETTLE_MS)


def explore_graph(page: Page) -> None:
    """The canvas, coloured by group, a core library open in the panel."""
    _go(page, "/explore")
    _mode(page, "Graph")
    page.get_by_role("button", name="Colours").click()
    page.get_by_text("By group", exact=True).click()
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)
    page.keyboard.press("/")
    page.keyboard.type("corelog")
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(SETTLE_MS)


def explore_groups(page: Page) -> None:
    """The communities of the last recompute, named after their hubs."""
    _go(page, "/explore")
    _mode(page, "Groups")


def explore_map(page: Page) -> None:
    """One bubble per label, a line per linked pair."""
    _go(page, "/explore")
    _mode(page, "Map")


def schema_editor(page: Page) -> None:
    """The schema diagram: a label selected (its lines, its panel), the Add menu open."""
    _go(page, "/schema")
    page.get_by_text("Diagram", exact=True).click()
    page.wait_for_timeout(SETTLE_MS)
    page.get_by_text("Selected", exact=True).click()
    page.get_by_text("Library", exact=True).first.click()
    page.wait_for_timeout(SETTLE_MS)
    page.get_by_role("button", name="Add").click()
    page.wait_for_timeout(1000)


SHOTS: dict[str, Callable[[Page], None]] = {
    "explore-graph": explore_graph,
    "explore-groups": explore_groups,
    "explore-map": explore_map,
    "schema-editor": schema_editor,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("shots", nargs="*", help=f"some of {', '.join(SHOTS)} (default: all)")
    ap.add_argument("--ui", default="http://localhost:5173", help="the UI's address")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    ap.add_argument("--channel", default=None, help="an installed browser: msedge, chrome")
    ap.add_argument("--scheme", choices=["dark", "light"], default="dark")
    args = ap.parse_args()
    user, password = os.environ.get("GRAPHOS_USER"), os.environ.get("GRAPHOS_PASSWORD")
    if not user or not password:
        print("set GRAPHOS_USER and GRAPHOS_PASSWORD", file=sys.stderr)
        return 2
    unknown = [name for name in args.shots if name not in SHOTS]
    if unknown:
        ap.error(f"unknown shot {', '.join(unknown)}; choose from {', '.join(SHOTS)}")
    names = args.shots or list(SHOTS)
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme=args.scheme)
        for name in names:
            # A fresh page per shot: each starts from the same state.
            page = ctx.new_page()
            page.goto(args.ui + "/")
            page.get_by_label("User name").fill(user)
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.wait_for_url("**/explore**")
            page.wait_for_timeout(SETTLE_MS)
            SHOTS[name](page)
            target = args.out / f"ui-{name}-{args.scheme}.png"
            page.screenshot(path=target)
            page.screenshot(path=target.with_suffix(".jpg"), type="jpeg", quality=JPEG_QUALITY)
            print(target.relative_to(ROOT) if target.is_relative_to(ROOT) else target)
            page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
