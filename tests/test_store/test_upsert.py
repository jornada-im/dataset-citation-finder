"""Tests for store.upsert helpers."""

from __future__ import annotations

import time

import pytest

from dataset_citation_finder import open_store
from dataset_citation_finder.schema import LongRow
from dataset_citation_finder.store.upsert import (
    bump_queried_dois,
    fetch_freshness,
    finish_run,
    insert_run,
    upsert_datacite_event,
    upsert_openalex,
    upsert_opencitations,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "store.sqlite"
    c = open_store(db)
    yield c
    c.close()


class TestRuns:

    def test_insert_and_finish(self, conn):
        run_id = insert_run(
            conn, input_kind="batch", input_value=None, input_count=3,
            sources_requested=["openalex", "opencitations"],
        )
        assert isinstance(run_id, int)
        finish_run(conn, run_id, sources_succeeded=["openalex"], rows_written=10)
        row = conn.execute(
            "SELECT input_kind, input_count, sources_requested, sources_succeeded, "
            "rows_written, finished_at FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row["input_kind"] == "batch"
        assert row["input_count"] == 3
        assert row["sources_requested"] == '["openalex", "opencitations"]'
        assert row["sources_succeeded"] == '["openalex"]'
        assert row["rows_written"] == 10
        assert row["finished_at"]


class TestQueriedDois:

    def test_bump_inserts(self, conn):
        run_id = insert_run(conn, input_kind="batch", input_value=None,
                            input_count=2, sources_requested=["openalex"])
        bump_queried_dois(conn, cited_dois=["10.1234/a", "10.1234/b"],
                          source="openalex", run_id=run_id)
        rows = conn.execute(
            "SELECT cited_doi, source, last_run_id FROM queried_dois ORDER BY cited_doi"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["cited_doi"] == "10.1234/a"
        assert rows[0]["source"] == "openalex"
        assert rows[0]["last_run_id"] == run_id

    def test_bump_updates_existing(self, conn):
        run1 = insert_run(conn, input_kind="batch", input_value=None,
                          input_count=1, sources_requested=["openalex"])
        bump_queried_dois(conn, cited_dois=["10.1234/a"], source="openalex", run_id=run1)
        first_ts = conn.execute(
            "SELECT last_queried_at FROM queried_dois WHERE cited_doi='10.1234/a'"
        ).fetchone()["last_queried_at"]

        time.sleep(0.01)
        run2 = insert_run(conn, input_kind="batch", input_value=None,
                          input_count=1, sources_requested=["openalex"])
        bump_queried_dois(conn, cited_dois=["10.1234/a"], source="openalex", run_id=run2)
        second = conn.execute(
            "SELECT last_queried_at, last_run_id FROM queried_dois WHERE cited_doi='10.1234/a'"
        ).fetchone()
        assert second["last_run_id"] == run2
        assert second["last_queried_at"] >= first_ts
        # Still only one row for that (doi, source).
        count = conn.execute("SELECT COUNT(*) FROM queried_dois").fetchone()[0]
        assert count == 1

    def test_fetch_freshness(self, conn):
        run = insert_run(conn, input_kind="batch", input_value=None,
                         input_count=2, sources_requested=["openalex"])
        bump_queried_dois(conn, cited_dois=["10.1234/a", "10.1234/b"],
                          source="openalex", run_id=run)
        out = fetch_freshness(conn, cited_dois=["10.1234/a", "10.1234/c"], source="openalex")
        assert "10.1234/a" in out
        assert "10.1234/c" not in out


class TestDataCiteUpsert:

    def test_insert_then_reinsert_preserves_first_advances_last(self, conn):
        run1 = insert_run(conn, input_kind="single", input_value="10.1234/a",
                          input_count=1, sources_requested=["datacite_events"])
        row = LongRow(
            cited_doi="10.1234/a", citing_doi="10.5678/b",
            source="datacite_events", relation_type="is-cited-by",
            extra={"event_id": "evt-1", "upstream_source": "crossref",
                   "occurred_at": "2025-01-01T00:00:00Z"},
        )
        upsert_datacite_event(conn, row, run1)
        first = conn.execute(
            "SELECT first_retrieved_at, last_retrieved_at, run_id "
            "FROM source_datacite_events WHERE event_id='evt-1'"
        ).fetchone()

        time.sleep(0.01)
        run2 = insert_run(conn, input_kind="single", input_value="10.1234/a",
                          input_count=1, sources_requested=["datacite_events"])
        upsert_datacite_event(conn, row, run2)
        second = conn.execute(
            "SELECT first_retrieved_at, last_retrieved_at, run_id "
            "FROM source_datacite_events WHERE event_id='evt-1'"
        ).fetchone()

        assert second["first_retrieved_at"] == first["first_retrieved_at"]
        assert second["last_retrieved_at"] >= first["last_retrieved_at"]
        assert second["run_id"] == run2

        # Still exactly one row.
        count = conn.execute("SELECT COUNT(*) FROM source_datacite_events").fetchone()[0]
        assert count == 1

    def test_skips_row_with_no_event_id(self, conn):
        run = insert_run(conn, input_kind="single", input_value="10.1234/a",
                         input_count=1, sources_requested=["datacite_events"])
        row = LongRow(
            cited_doi="10.1234/a", citing_doi="10.5678/b",
            source="datacite_events", relation_type="is-cited-by",
            extra={},  # no event_id
        )
        upsert_datacite_event(conn, row, run)
        count = conn.execute("SELECT COUNT(*) FROM source_datacite_events").fetchone()[0]
        assert count == 0


class TestOpenAlexUpsert:

    def test_insert_then_reinsert(self, conn):
        run1 = insert_run(conn, input_kind="single", input_value="10.1234/a",
                          input_count=1, sources_requested=["openalex"])
        row = LongRow(
            cited_doi="10.1234/a", citing_doi="10.5678/b", source="openalex",
            extra={"openalex_id": "https://openalex.org/W123"},
        )
        upsert_openalex(conn, row, run1)
        first = conn.execute(
            "SELECT first_retrieved_at, last_retrieved_at FROM source_openalex"
        ).fetchone()

        time.sleep(0.01)
        run2 = insert_run(conn, input_kind="single", input_value="10.1234/a",
                          input_count=1, sources_requested=["openalex"])
        upsert_openalex(conn, row, run2)
        second = conn.execute(
            "SELECT first_retrieved_at, last_retrieved_at, citing_openalex_id, run_id "
            "FROM source_openalex"
        ).fetchone()
        assert second["first_retrieved_at"] == first["first_retrieved_at"]
        assert second["last_retrieved_at"] >= first["last_retrieved_at"]
        assert second["citing_openalex_id"] == "https://openalex.org/W123"
        assert second["run_id"] == run2


class TestOpenCitationsUpsert:

    def test_insert(self, conn):
        run = insert_run(conn, input_kind="single", input_value="10.1234/a",
                        input_count=1, sources_requested=["opencitations"])
        row = LongRow(
            cited_doi="10.1234/a", citing_doi="10.5678/b", source="opencitations",
            extra={"creation": "2024-03-01", "journal_sc": "no", "author_sc": "no"},
        )
        upsert_opencitations(conn, row, run)
        r = conn.execute("SELECT * FROM source_opencitations").fetchone()
        assert r["creation_date"] == "2024-03-01"
        assert r["journal_sc"] == "no"
