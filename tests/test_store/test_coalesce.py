"""Tests for store.coalesce.coalesce_citations."""

from __future__ import annotations

import json

import pytest

from dataset_citation_finder import coalesce_citations, open_store
from dataset_citation_finder.schema import LongRow
from dataset_citation_finder.store.upsert import (
    insert_run,
    upsert_datacite_event,
    upsert_openalex,
    upsert_opencitations,
)


@pytest.fixture
def populated_store(tmp_path):
    """A store with a few rows in each source table."""
    db = tmp_path / "store.sqlite"
    conn = open_store(db)
    run = insert_run(conn, input_kind="batch", input_value=None,
                     input_count=2, sources_requested=["datacite_events", "openalex", "opencitations"])

    upsert_datacite_event(conn, LongRow(
        cited_doi="10.6073/a", citing_doi="10.1038/x",
        source="datacite_events", relation_type="is-cited-by",
        extra={"event_id": "evt-1"},
    ), run)
    upsert_datacite_event(conn, LongRow(
        cited_doi="10.6073/a", citing_doi="10.1038/x",
        source="datacite_events", relation_type="references",
        extra={"event_id": "evt-2"},
    ), run)
    upsert_openalex(conn, LongRow(
        cited_doi="10.6073/a", citing_doi="10.1038/x", source="openalex",
        extra={"openalex_id": "W1"},
    ), run)
    upsert_opencitations(conn, LongRow(
        cited_doi="10.6073/a", citing_doi="10.1038/y", source="opencitations",
    ), run)

    yield db, conn
    conn.close()


class TestCoalesceCitations:

    def test_aggregates_sources_per_edge(self, populated_store):
        db, _ = populated_store
        n = coalesce_citations(db)
        assert n == 2  # two distinct (cited, citing) edges

        conn = open_store(db)
        try:
            rows = conn.execute(
                "SELECT cited_doi, citing_doi, sources, source_count, relation_types "
                "FROM citations ORDER BY citing_doi"
            ).fetchall()
        finally:
            conn.close()

        edge1 = next(r for r in rows if r["citing_doi"] == "10.1038/x")
        sources = json.loads(edge1["sources"])
        assert set(sources) == {"datacite_events", "openalex"}
        assert edge1["source_count"] == 2
        rels = json.loads(edge1["relation_types"])
        assert set(rels) == {"is-cited-by", "references"}

        edge2 = next(r for r in rows if r["citing_doi"] == "10.1038/y")
        assert json.loads(edge2["sources"]) == ["opencitations"]
        assert edge2["source_count"] == 1

    def test_rebuild_replaces_old_rows(self, populated_store):
        """Re-running coalesce should not duplicate; truncate-then-insert."""
        db, _ = populated_store
        coalesce_citations(db)
        coalesce_citations(db)
        conn = open_store(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_empty_store_yields_empty_table(self, tmp_path):
        db = tmp_path / "empty.sqlite"
        open_store(db).close()
        n = coalesce_citations(db)
        assert n == 0

    def test_accepts_open_connection(self, populated_store):
        db, conn = populated_store
        n = coalesce_citations(conn)
        assert n == 2
