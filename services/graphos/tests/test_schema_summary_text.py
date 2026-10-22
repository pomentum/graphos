"""The schema summary's text form, without a database."""

from __future__ import annotations

from graphos.api.schema_summary import _render
from graphos.contracts.schema_summary import SchemaSummaryResponse


def _summary(computed: list[str]) -> SchemaSummaryResponse:
    return SchemaSummaryResponse(
        tenant="acme", labels=[], labels_total=0, links=[], links_total=0, computed=computed,
        estimated=False, text="", cached=False, elapsed_ms=0,
    )


def test_the_computed_line_says_how_to_read_an_edges_communities() -> None:
    line = next(x for x in _render(_summary(["community_id", "graphos_pagerank"])).splitlines()
                if x.startswith("Computed by graphos"))
    assert line.startswith("Computed by graphos (read-only): community_id, graphos_pagerank; ")
    assert "RETURN a.community_id, b.community_id" in line


def test_without_communities_the_computed_line_is_as_before() -> None:
    text = _render(_summary(["graphos_degree"]))
    assert "Computed by graphos (read-only): graphos_degree\n" in text
    assert "community_id" not in text
