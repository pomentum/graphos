"""Build the graphos website, https://pomentum.github.io/graphos/, into website/.build/site.

The site is one page and the whitepaper. The page is ``website/index.html``, a frame
whose ``{{ name }}`` slots this script fills from files the repository already has, so
nothing on it is written twice:

- the whitepaper's stylesheet and theme script, so the page and the paper share their
  colours, type and light/dark switch (and the reader's pick, kept under the same key);
- the whitepaper's "In 30 seconds" summary;
- the README's features (Build, Explore, Run), web UI screenshots and captions, first
  quickstart block and documentation table;
- the version from ``services/graphos/pyproject.toml``.

The whitepaper is published as it is, at ``whitepaper/``. Links into the repository
(``docs/usage.md``) point to GitHub. Pictures come from ``docs/images/``.

The build fails when a slot is left unfilled or a local link or picture points at a file
the site does not have: the check ``mkdocs --strict`` would have made. It uses the
standard library only, so CI needs nothing but Python.
"""

from __future__ import annotations

import html
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
WEBSITE = REPO / "website"
OUT = WEBSITE / ".build" / "site"
GITHUB = "https://github.com/pomentum/graphos"
# Pages of the site that README links may point to, by repository path.
ON_SITE = {"docs/whitepaper.html": "whitepaper/"}
# README sections the page does not show, and where they live instead.
README_ANCHORS = {"#python-client": f"{GITHUB}/tree/main/clients/python"}
DOCS_COLUMNS = 2  # the README's documentation table: | Doc | Read it for |
WHITEPAPER_SCRIPTS = 2  # the theme script in its head, the switch's script at its end


def repo_link(target: str, base: str = "") -> str:
    """Return a link from a README or doc, made to work from the site's root."""
    if target in README_ANCHORS:
        return README_ANCHORS[target]
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
        return target
    path, hash_, fragment = target.partition("#")
    path = str(Path(base, path).as_posix()).removeprefix("./")
    if path in ON_SITE:
        return ON_SITE[path] + hash_ + fragment
    if path.startswith("docs/images/"):
        return path.removeprefix("docs/")
    kind = "tree" if (REPO / path).is_dir() else "blob"
    return f"{GITHUB}/{kind}/main/{path}{hash_}{fragment}"


def inline(md: str) -> str:
    """Render the inline Markdown the README uses: code, bold, italics and links."""
    out: list[str] = []
    for i, part in enumerate(re.split(r"(`[^`]+`)", md)):
        if i % 2:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        text = html.escape(part, quote=False)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r"<em>\1</em>", text)
        text = re.sub(
            r"\[([^\]]+)\]\(([^)\s]+)\)",
            lambda m: f'<a href="{html.escape(repo_link(m.group(2)))}">{m.group(1)}</a>',
            text,
        )
        out.append(text)
    return "".join(out)


def sections(markdown: str) -> dict[str, str]:
    """Split a Markdown file into its ``##`` sections, by lower-case title."""
    parts: dict[str, str] = {}
    for block in markdown.split("\n## ")[1:]:
        title, _, body = block.partition("\n")
        parts[title.strip().lower()] = body
    return parts


def features(body: str) -> str:
    """The README's Build / Explore / Run lists, as groups of cards."""
    out: list[str] = []
    for line in body.splitlines():
        group = re.fullmatch(r"\*\*(\w+)\*\*", line.strip())
        item = re.match(r"- \*\*(.+?):\*\* (.*)", line)
        if group:
            if out:
                out.append("</div></div>")
            out.append(f'<div class="group"><h3>{group.group(1)}</h3><div class="cards">')
        elif item:
            text = item.group(2)[:1].upper() + item.group(2)[1:]
            out.append(
                f'<div class="card"><h4>{inline(item.group(1))}</h4><p>{inline(text)}</p></div>'
            )
    out.append("</div></div>")
    return "\n".join(out)


def shots(body: str) -> str:
    """The README's web UI screenshots, each with its alt text and caption."""
    out: list[str] = []
    pairs = re.findall(
        r'<img src="([^"]+)" width="100%" alt="([^"]+)">\s*\n\s*\*(.+?)\*\s*\n', body
    )
    for n, (src, alt, caption) in enumerate(pairs, 1):
        out.append(
            f'<figure class="fig"><img class="shot" src="{repo_link(src)}" alt="{alt}" '
            f'loading="lazy" width="1440" height="900"><figcaption><span class="fig-n">'
            f"UI {n:02d}</span><span>{inline(caption)}</span></figcaption></figure>"
        )
    return "\n".join(out)


def first_code_block(body: str) -> str:
    """The first fenced code block of a section, escaped for a ``<pre>``."""
    m = re.search(r"```\w*\n(.*?)\n```", body, re.DOTALL)
    if not m:
        raise SystemExit("README Quickstart has no code block")
    return html.escape(m.group(1))


def docs_table(body: str) -> str:
    """The README's documentation table, as rows of links and what each is for."""
    rows: list[str] = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != DOCS_COLUMNS or not cells[0].startswith("["):
            continue
        name = re.match(r"\[([^\]]+)\]\(([^)]+)\)", cells[0])
        if not name:
            continue
        label, target = name.groups()
        if "vercel.app" in target:  # the whitepaper now lives on this site
            target = "docs/whitepaper.html"
        rows.append(
            f'<div><dt><a href="{repo_link(target)}">{html.escape(label)}</a></dt>'
            f"<dd>{inline(cells[1])}</dd></div>"
        )
    return "\n".join(rows)


def whitepaper_parts(paper: str) -> dict[str, str]:
    """The whitepaper's stylesheet, its theme scripts and its summary section."""
    style = re.search(r"<style>(.*?)</style>", paper, re.DOTALL)
    scripts = re.findall(r"<script>(.*?)</script>", paper, re.DOTALL)
    summary = re.search(r'<section class="summary" id="summary">.*?</section>', paper, re.DOTALL)
    if not (style and len(scripts) == WHITEPAPER_SCRIPTS and summary):
        raise SystemExit("whitepaper: stylesheet, its two scripts or summary not found")
    return {
        "style": style.group(1),
        "theme_script": scripts[0],
        "page_script": scripts[1],
        "summary": summary.group(0),
    }


def version() -> str:
    """The version the service, client and UI share."""
    text = (REPO / "services" / "graphos" / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("no version in services/graphos/pyproject.toml")
    return m.group(1)


def check_links(page: str) -> list[str]:
    """Local links and pictures of the page that the site does not have."""
    missing: list[str] = []
    for target in re.findall(r'(?:href|src)="([^"]+)"', page):
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        path = OUT / url.path
        if url.path.endswith("/"):
            path = path / "index.html"
        if not path.exists():
            missing.append(target)
    return missing


def main() -> int:
    readme = sections((REPO / "README.md").read_text(encoding="utf-8"))
    paper = (REPO / "docs" / "whitepaper.html").read_text(encoding="utf-8")
    values = {
        **whitepaper_parts(paper),
        "version": version(),
        "features": features(readme["features"]),
        "shots": shots(readme["web ui"]),
        "quickstart": first_code_block(readme["quickstart"]),
        "docs": docs_table(readme["documentation"]),
    }

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "whitepaper").mkdir(parents=True)
    shutil.copytree(REPO / "docs" / "images", OUT / "images")
    shutil.copyfile(REPO / "docs" / "whitepaper.html", OUT / "whitepaper" / "index.html")
    shutil.copyfile(WEBSITE / "favicon.svg", OUT / "favicon.svg")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    frame = (WEBSITE / "index.html").read_text(encoding="utf-8")
    page = re.sub(r"\{\{ (\w+) \}\}", lambda m: values[m.group(1)], frame)
    if leftover := re.findall(r"\{\{ \w+ \}\}", page):
        raise SystemExit(f"unfilled slots: {leftover}")
    (OUT / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (OUT / "404.html").write_text(
        (WEBSITE / "404.html").read_text(encoding="utf-8").replace("{{ style }}", values["style"]),
        encoding="utf-8",
        newline="\n",
    )

    if missing := check_links(page):
        print("links to files the site does not have:", *missing, sep="\n  ", file=sys.stderr)
        return 1
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"built {OUT.relative_to(REPO).as_posix()} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
