"""A demo tenant: an invented open-source software ecosystem, then its analytics.

About 450 packages, maintainers, teams, organisations and topics in six ecosystems (web,
frontend, data, machine learning, operations, security), joined by a few
shared core libraries and some cross-ecosystem dependencies.  Every name is
made up.  The graph is built so the algorithms have something to find:

- **communities (Louvain)** recover the six ecosystems from the edges alone;
- **PageRank** ranks the core libraries that everything depends on first;
- **betweenness** finds the packages that bridge two ecosystems;
- **components** finds the few packages nothing connects to the rest.

It also declares how the web UI draws each label (colours, icons, sizes) and,
when the server's Cypher console is on, saves a few Cypher queries for the
Query page.

It is the data behind the screenshots in the docs, and a good tenant to try
the web UI on.  Re-running it rewrites the same graph (upserts are keyed on
ids), and the metrics re-run writes nothing when nothing changed.

    export GRAPHOS_URL=http://127.0.0.1:8080
    export GRAPHOS_USER=graphos GRAPHOS_PASSWORD=...   # or a service account
    python clients/python/examples/08_demo_ecosystem.py
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Iterable, Sequence
from http import HTTPStatus
from typing import TypeVar

from graphos_client import EdgeUpsert, GraphosClient, GraphosError, NodeUpsert

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TENANT = os.environ.get("GRAPHOS_DEMO_TENANT", "demo")
# Signs in as GRAPHOS_USER / GRAPHOS_PASSWORD and renews its token by itself.

T = TypeVar("T")

# ── The ecosystem, generated from a fixed seed ───────────────────────────────

ECOSYSTEMS: dict[str, dict[str, Sequence[str]]] = {
    "web": {
        "stems": ["route", "session", "socket", "cookie", "header", "proxy", "form", "template"],
        "topics": ["http", "servers", "templating"],
        "language": ["Python", "Go"],
    },
    "frontend": {
        "stems": ["pixel", "canvas", "widget", "layout", "style", "theme", "icon", "motion"],
        "topics": ["ui-components", "css", "animation"],
        "language": ["TypeScript"],
    },
    "data": {
        "stems": ["table", "column", "frame", "query", "ledger", "batch", "stream", "shard"],
        "topics": ["dataframes", "sql", "streaming"],
        "language": ["Python", "Rust"],
    },
    "ml": {
        "stems": ["tensor", "gradient", "vector", "neuron", "embed", "train", "kernel", "sample"],
        "topics": ["deep-learning", "embeddings", "training"],
        "language": ["Python", "C++"],
    },
    "ops": {
        "stems": ["deploy", "ship", "build", "pipeline", "runner", "probe", "metric", "scale"],
        "topics": ["ci-cd", "monitoring", "containers"],
        "language": ["Go", "Rust"],
    },
    "security": {
        "stems": ["cipher", "vault", "token", "audit", "signer", "guard", "shield", "hash"],
        "topics": ["cryptography", "authentication", "compliance"],
        "language": ["Rust", "Go"],
    },
}
SUFFIXES = ["kit", "ly", "ora", "wise", "ium", "forge", "nest", "flow", "lite", "craft"]
# Ecosystems whose packages sometimes depend on each other's: the bridges.
NEIGHBOURS = [("web", "frontend"), ("data", "ml"), ("ops", "security"), ("web", "data"),
              ("ml", "ops")]
CORE = ["corelog", "confy", "jsonette", "timewise", "retryly", "pathkit", "fmtcore", "testbench"]
LICENSES = ["MIT", "Apache-2.0", "BSD-3-Clause", "MPL-2.0"]
FIRST = ["Amara", "Bruno", "Chen", "Dana", "Elif", "Farid", "Greta", "Hiro", "Ines", "Jonas",
         "Kemi", "Lars", "Maya", "Nikos", "Oona", "Pavel", "Quinn", "Rosa", "Sami", "Tara",
         "Umar", "Vera", "Wren", "Yusuf", "Zoe"]
LAST = ["Arden", "Brook", "Calder", "Dune", "Ember", "Frost", "Gale", "Heath", "Isle",
        "Juniper", "Kestrel", "Linden", "Moss", "North", "Orchard", "Pike", "Quarry", "Reed",
        "Stone", "Thorne"]
PACKAGES_PER_ECOSYSTEM = 45
PEOPLE = 130
USES_CORE = 0.45            # share of packages that depend on a core library
MAINTAINS_CORE = 0.15       # share of people who also maintain one
# Kinds of package: labels under Package (a read of Package sees them all).
KINDS = ["Library", "Framework", "Tool"]
ORGANISATIONS = {"Cedar Commons": ["web", "frontend"],
                 "Bluefjord Foundation": ["data", "ml"],
                 "Atlas Guild": ["ops", "security"]}


def _kind(index: int) -> str:
    """Every sixth package of an ecosystem is a framework, a few are tools."""
    if index % 6 == 5:          # noqa: PLR2004 — the demo's mix
        return "Framework"
    return "Tool" if index % 6 == 2 else "Library"  # noqa: PLR2004


class _Builder:
    """Collects the demo's nodes and edges, drawing from one seeded generator."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.nodes: list[NodeUpsert] = []
        self.edges: list[EdgeUpsert] = []
        self.kind_of: dict[str, str] = {}          # package id -> its label
        self.core: list[str] = []
        self.by_eco: dict[str, list[str]] = {}

    def node(self, id_: str, label: str, **properties: object) -> str:
        self.nodes.append(NodeUpsert(id=id_, label=label, properties=properties))
        return id_

    def edge(self, src: str, src_label: str, type_: str, dst: str, dst_label: str) -> None:
        self.edges.append(EdgeUpsert(src_id=src, src_label=src_label, dst_id=dst,
                                     dst_label=dst_label, type=type_))

    def package(self, name: str, kind: str, language: str) -> str:
        rng = self.rng
        self.kind_of[f"pkg:{name}"] = kind
        return self.node(
            f"pkg:{name}", kind, name=name,
            version=f"{rng.randint(0, 4)}.{rng.randint(0, 19)}.{rng.randint(0, 9)}",
            license=rng.choice(LICENSES), language=language,
            downloads_month=int(rng.lognormvariate(9, 1.6)),
            first_release=rng.randint(2012, 2025),
        )

    def depends(self, src: str, dst: str) -> None:
        if src != dst:
            self.edge(src, self.kind_of[src], "DEPENDS_ON", dst, self.kind_of[dst])

    def ecosystems(self) -> None:
        """Packages, topics and teams per ecosystem; organisations over teams."""
        self.core = [self.package(name, "Library", "Python") for name in CORE]
        for eco, spec in ECOSYSTEMS.items():
            names = [s + x for s in spec["stems"] for x in SUFFIXES]
            self.rng.shuffle(names)
            self.by_eco[eco] = [self.package(n, _kind(i), self.rng.choice(spec["language"]))
                                for i, n in enumerate(names[:PACKAGES_PER_ECOSYSTEM])]
            for topic in spec["topics"]:
                self.node(f"topic:{topic}", "Topic", name=topic)
            for team in (f"{eco}-core", f"{eco}-tools"):
                self.node(f"team:{team}", "Team", name=team)
        for org, ecos in ORGANISATIONS.items():
            org_id = self.node("org:" + org.lower().replace(" ", "-"), "Organisation", name=org)
            for eco in ecos:
                for part in ("core", "tools"):
                    self.edge(f"team:{eco}-{part}", "Team", "PART_OF", org_id, "Organisation")

    def dependencies(self) -> None:
        """Within an ecosystem, on the core libraries, and a few across: the bridges."""
        rng = self.rng
        for eco, pkgs in self.by_eco.items():
            for i, pkg in enumerate(pkgs):
                # Depend on earlier packages of the same ecosystem: its first
                # ones become its foundations.
                for target in rng.sample(pkgs[:max(i, 1)], k=min(i, rng.randint(2, 5))):
                    self.depends(pkg, target)
                if rng.random() < USES_CORE:
                    self.depends(pkg, rng.choice(self.core))
                for topic in rng.sample(list(ECOSYSTEMS[eco]["topics"]), k=rng.randint(1, 2)):
                    self.edge(pkg, self.kind_of[pkg], "TAGGED", f"topic:{topic}", "Topic")
        for a, b in NEIGHBOURS:
            for _ in range(6):
                self.depends(rng.choice(self.by_eco[a]), rng.choice(self.by_eco[b][:10]))
        for pkg in self.core[1:]:
            self.depends(pkg, self.core[0])            # everything logs
        # Two packages that only depend on each other: a component of their own.
        self.depends(self.package("orphanly", "Tool", "Python"),
                     self.package("lonekit", "Library", "Python"))

    def people(self) -> None:
        """Maintainers, each in a team of an ecosystem, maintaining a few of its packages."""
        rng = self.rng
        names = rng.sample([f"{f} {surname}" for f in FIRST for surname in LAST], k=PEOPLE)
        ecos = list(ECOSYSTEMS)
        for n, name in enumerate(names):
            handle = name.lower().replace(" ", ".")
            eco = ecos[n % len(ecos)]
            person = self.node(f"person:{handle}", "Person", name=name, handle=handle)
            self.edge(person, "Person", "MEMBER_OF",
                      f"team:{eco}-{rng.choice(['core', 'tools'])}", "Team")
            owned = rng.sample(self.by_eco[eco], k=rng.randint(1, 3))
            if rng.random() < MAINTAINS_CORE:
                owned.append(rng.choice(self.core))
            for pkg in owned:
                self.edge(person, "Person", "MAINTAINS", pkg, self.kind_of[pkg])


# How the web UI draws the demo: a colour and an icon per label, packages sized
# by PageRank (once metrics ran).  Every reader of the tenant sees the same.
DISPLAY = {
    "vertex": {
        "Library": {"color": "#6F8FC2", "icon": "code", "size_by": "graphos_pagerank"},
        "Framework": {"color": "#A98AD0", "icon": "box", "size_by": "graphos_pagerank"},
        "Tool": {"color": "#5BA9BF", "icon": "settings", "size_by": "graphos_pagerank"},
        "Person": {"color": "#C9825B", "icon": "person"},
        "Team": {"color": "#C7AE55", "icon": "people"},
        "Organisation": {"color": "#B98D63", "icon": "building"},
        "Topic": {"color": "#8DA85A", "icon": "tag"},
    },
    "edge": {
        "MAINTAINS": {"color": "#C9825B"},
        "TAGGED": {"color": "#8DA85A"},
    },
}

# Saved Cypher queries for the Query page (read-only; hops written out, as the
# console runs no variable-length patterns).
QUERIES: dict[str, dict[str, object]] = {
    "most_depended_on": {
        "description": "The packages most others depend on",
        "query": "MATCH (d:Package)-[:DEPENDS_ON]->(p:Package) "
                 "RETURN p.name AS package, count(d) AS dependents ORDER BY count(d) DESC LIMIT 15",
    },
    "maintainers_of": {
        "description": "Who maintains a package, and their teams",
        "query": "MATCH (m:Person)-[r:MAINTAINS]->(p:Package {name: $package}) "
                 "OPTIONAL MATCH (m)-[t:MEMBER_OF]->(team:Team) RETURN m, r, p, t, team",
        "params": {"package": {"type": "string", "label": "Package name"}},
        "show": "graph",
    },
    "dependencies_of": {
        "description": "A package's dependencies, two levels deep",
        "query": "MATCH (p:Package {name: $package})-[r1:DEPENDS_ON]->(d:Package) "
                 "OPTIONAL MATCH (d)-[r2:DEPENDS_ON]->(e:Package) RETURN p, r1, d, r2, e LIMIT 200",
        "params": {"package": {"type": "string", "label": "Package name"}},
        "show": "graph",
    },
    "licences_by_language": {
        "description": "Licences in use, by language",
        "query": "MATCH (p:Package) RETURN p.language AS language, p.license AS license, "
                 "count(*) AS packages ORDER BY count(*) DESC LIMIT 20",
    },
    "bridges": {
        "description": "Packages that bridge ecosystems (betweenness)",
        "query": "MATCH (p:Package) WHERE p.graphos_betweenness > 0 RETURN p.name AS package, "
                 "label(p) AS kind, p.graphos_betweenness AS betweenness "
                 "ORDER BY p.graphos_betweenness DESC LIMIT 10",
    },
}


def build(seed: int = 2026) -> tuple[list[NodeUpsert], list[EdgeUpsert]]:
    """The demo graph: the same nodes and edges for the same seed."""
    b = _Builder(seed)
    b.ecosystems()
    b.dependencies()
    b.people()
    return b.nodes, b.edges


def chunks(items: Sequence[T], size: int = 500) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def main() -> None:
    nodes, edges = build()
    async with GraphosClient(base_url=URL, tenant=TENANT) as g:
        await g.register_tenant(
            vertex_labels=["Package", *KINDS, "Person", "Team", "Organisation", "Topic"],
            edge_labels=["DEPENDS_ON", "MAINTAINS", "MEMBER_OF", "PART_OF", "TAGGED"],
            # Library, Framework and Tool are kinds of Package: a pair declared
            # for Package holds for them, and reads of Package include them.
            label_hierarchy=dict.fromkeys(KINDS, "Package"),
            edge_endpoints={
                "DEPENDS_ON": [("Package", "Package")],
                "MAINTAINS": [("Person", "Package")],
                "MEMBER_OF": [("Person", "Team")],
                "PART_OF": [("Team", "Organisation")],
                "TAGGED": [("Package", "Topic")],
            },
            search_indexes={"names": {"labels": ["*"], "properties": ["name", "handle"],
                                      "analyzer": "identifier"}},
            meta={"description": "demo: an invented open-source software ecosystem"},
            display=DISPLAY,
        )
        for batch in chunks(nodes):
            await g.upsert_nodes(batch)
        for batch in chunks(edges):
            await g.upsert_edges(batch)
        print(f"{TENANT}: {len(nodes)} nodes, {len(edges)} edges")

        # Metrics: always available.  Only dependencies count for the scores.
        run = await g.compute_metrics(
            metrics=["degree", "pagerank", "components", "betweenness"],
            rel_types=["DEPENDS_ON"],
        )
        print(f"metrics: {run.n_nodes} packages, {run.n_components} components, "
              f"{run.n_changed} values written")
        for prop, what in (("graphos_pagerank", "most depended on (PageRank)"),
                           ("graphos_betweenness", "bridges (betweenness)")):
            top = await g.scan_nodes(labels=["Package"], project=["name", prop], limit=5,
                                     order_by=[{"property": prop, "desc": True}])
            print(f"{what}:", ", ".join(n.properties["name"] for n in top.items))

        # Saved queries: only where the server's Cypher console is on.
        if (await g.info()).get("features", {}).get("cypher"):
            for name, saved in QUERIES.items():
                await g.save_query(name, **saved)  # type: ignore[arg-type]
            print(f"saved queries: {', '.join(QUERIES)}")

        # Communities: needs the Louvain extra.
        if not (await g.info()).get("features", {}).get("louvain"):
            print("this server was built without the Louvain extra: no communities")
            return
        try:
            found = await g.recompute_communities(
                rel_types=["DEPENDS_ON", "MAINTAINS", "MEMBER_OF", "TAGGED"],   # not PART_OF
                rel_weights={"DEPENDS_ON": 1.0, "MAINTAINS": 0.5, "MEMBER_OF": 0.5,
                             "TAGGED": 0.3},
            )
        except GraphosError as exc:
            if exc.status_code == HTTPStatus.CONFLICT:
                print("a whole-graph job is already running for this tenant; try again later")
                return
            raise
        print(f"communities: {found.n_communities}, modularity {found.modularity:.3f}")
        community_map = await g.community_map()
        for c in community_map.communities[:8]:
            hub = c.hub.properties.get("name") if c.hub else "?"
            print(f"  {c.community_id:>3}  {c.size:>4} members, around {hub}")


if __name__ == "__main__":
    asyncio.run(main())
