"""Assemble the graphos website from the docs the repository already has.

The site has no content of its own beyond the landing page's frame: every page
is a file that lives in the repository (``docs/*.md``, the component READMEs,
the changelog) and the landing page takes its text from the README. This script
copies them into ``website/.build/docs`` at build time and fixes their links,
so the Markdown in the repository stays the single source and keeps reading
well on GitHub.

Links are rewritten so they work on the site:

- a link to a file the site includes points to that page or picture;
- a link to anything else in the repository points to it on GitHub;
- a ``<picture>`` that switches on ``prefers-color-scheme`` becomes two images
  that Material shows by the site's own light/dark toggle (``#only-light``,
  ``#only-dark``).

Code blocks are left as they are. The script uses the standard library only and
writes plain Markdown, so the build directory works with MkDocs and with
Zensical, which reads the same ``mkdocs.yml``.

``docs/design/`` is local working material and is never copied.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEBSITE = REPO / "website"
OUT = WEBSITE / ".build" / "docs"
GITHUB = "https://github.com/pomentum/graphos"

# Repository files that become pages, besides docs/*.md, and where they go.
EXTRA_PAGES = {
    "CHANGELOG.md": "changelog.md",
    "services/graphos-ui/README.md": "ui.md",
    "clients/python/README.md": "client.md",
    "docs/adrs/README.md": "adrs/index.md",
}
README_ANCHORS = {"python-client": "clients/python/README.md"}
# docs/README.md is the repository's index of the docs; the site's navigation
# does that job, so it is not a page.
SKIPPED = {"docs/README.md"}

FENCE = re.compile(r"^\s*(```|~~~)")
MD_LINK = re.compile(r"(\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")
HTML_ATTR = re.compile(r"\b(src|srcset|href)=\"([^\"]+)\"")
PICTURE = re.compile(
    r"<picture>\s*<source media=\"\(prefers-color-scheme: dark\)\" srcset=\"([^\"]+)\">\s*"
    r"<img src=\"([^\"]+)\"([^>]*)>\s*</picture>",
    re.DOTALL,
)


def site_map() -> dict[str, str]:
    """Map each repository path the site includes to its path in the site."""
    mapping: dict[str, str] = {}
    for md in sorted((REPO / "docs").glob("*.md")):
        rel = md.relative_to(REPO).as_posix()
        if rel not in SKIPPED:
            mapping[rel] = md.name
    mapping.update(EXTRA_PAGES)
    for asset in sorted((REPO / "docs" / "images").rglob("*")):
        if asset.is_file():
            rel = asset.relative_to(REPO).as_posix()
            mapping[rel] = rel.removeprefix("docs/")
    mapping["docs/whitepaper.html"] = "whitepaper.html"
    return mapping


def rewrite_target(target: str, src: str, dest: str, mapping: dict[str, str]) -> str:
    """Return ``target``, a link in repository file ``src``, as seen from site page ``dest``."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/", "{")):
        return target
    path, hash_, fragment = target.partition("#")
    if not path:
        return target
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
    if resolved.startswith("..") or "<" in resolved:
        return target
    suffix = hash_ + fragment
    if resolved in mapping:
        rel = posixpath.relpath(mapping[resolved], posixpath.dirname(dest) or ".")
        return rel + suffix
    kind = "tree" if (REPO / resolved).is_dir() else "blob"
    return f"{GITHUB}/{kind}/main/{resolved}{suffix}"


def rewrite_prose(text: str, src: str, dest: str, mapping: dict[str, str]) -> str:
    """Fix the pictures and links of Markdown outside code blocks."""

    def picture(m: re.Match[str]) -> str:
        dark, light, rest = m.group(1), m.group(2), m.group(3)
        return f'<img src="{light}#only-light"{rest}>\n<img src="{dark}#only-dark"{rest}>'

    def md_link(m: re.Match[str]) -> str:
        return m.group(1) + rewrite_target(m.group(2), src, dest, mapping) + m.group(3)

    def html_attr(m: re.Match[str]) -> str:
        return f'{m.group(1)}="{rewrite_target(m.group(2), src, dest, mapping)}"'

    text = PICTURE.sub(picture, text)
    text = MD_LINK.sub(md_link, text)
    return HTML_ATTR.sub(html_attr, text)


def rewrite(text: str, src: str, dest: str, mapping: dict[str, str]) -> str:
    """Rewrite a whole Markdown file, leaving fenced code blocks untouched."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line)
        if fence is None and m:
            out.append(rewrite_prose("".join(prose), src, dest, mapping))
            prose = []
            fence = m.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
        else:
            prose.append(line)
    out.append(rewrite_prose("".join(prose), src, dest, mapping))
    return "".join(out)


def readme_sections(readme: str) -> dict[str, str]:
    """Split the README into the parts the landing page shows."""
    parts: dict[str, str] = {}
    head, _, rest = readme.partition("\n## ")
    paragraphs = [p.strip() for p in head.split("\n\n") if p.strip()]
    prose = [p for p in paragraphs if not p.startswith(("#", "[![", "<", "**Why"))]
    parts["tagline"] = prose[0]
    parts["summary"] = "\n\n".join(prose[1:])
    parts["figures"] = "\n\n".join(p for p in paragraphs if p.startswith("<"))
    for block in ("\n## " + rest).split("\n## ")[1:]:
        title, _, body = block.partition("\n")
        body = re.split(r"\n#{2,3} ", "\n" + body)[0]
        parts[title.strip().lower()] = body.strip()
    return parts


def feature_cards(features: str) -> str:
    """Turn the README's Build / Explore / Run lists into card grids."""
    out: list[str] = []
    for line in features.splitlines():
        group = re.fullmatch(r"\*\*(\w+)\*\*", line.strip())
        item = re.match(r"- \*\*(.+?):\*\* (.*)", line)
        if group:
            if out:
                out.append("</div>\n")
            out.append(f'<h3 class="gx-group">{group.group(1)}</h3>\n')
            out.append('<div class="gx-grid" markdown>\n')
        elif item:
            # The README's "Title: what it does" reads as a sentence on a card.
            text = item.group(2)[:1].upper() + item.group(2)[1:]
            out.append(
                f'<div class="gx-card" markdown>\n<p class="gx-card-title">{item.group(1)}</p>\n\n'
                f"{text}\n\n</div>\n"
            )
    out.append("</div>\n")
    return "\n".join(out)


def version() -> str:
    """Return the version the service, client and UI share."""
    pyproject = (REPO / "services" / "graphos" / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    return m.group(1) if m else "unknown"


def landing(mapping: dict[str, str]) -> str:
    """Fill the landing page's frame with the README's own text."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    parts = readme_sections(readme)
    values = {
        "tagline": parts["tagline"],
        "summary": parts["summary"],
        "figures": parts["figures"],
        "features": feature_cards(parts["features"]),
        "webui": parts["web ui"],
        "quickstart": parts["quickstart"],
        "version": version(),
    }
    frame = (WEBSITE / "home.md").read_text(encoding="utf-8")
    page = re.sub(r"\{\{ readme\.(\w+) \}\}", lambda m: values[m.group(1)], frame)
    # README sections the landing page does not show have a page of their own.
    for anchor, target in README_ANCHORS.items():
        page = page.replace(f"](#{anchor})", f"]({target})")
    return rewrite(page, "README.md", "index.md", mapping)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    mapping = site_map()
    for src, dest in mapping.items():
        target = OUT / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.endswith(".md"):
            text = (REPO / src).read_text(encoding="utf-8")
            target.write_text(rewrite(text, src, dest, mapping), encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(REPO / src, target)
    (OUT / "index.md").write_text(landing(mapping), encoding="utf-8", newline="\n")
    shutil.copytree(WEBSITE / "assets", OUT / "assets")
    print(f"assembled {len(mapping) + 1} files into {OUT.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
