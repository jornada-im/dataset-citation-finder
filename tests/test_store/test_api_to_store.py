"""End-to-end tests for the SQLite-backed API paths.

Uses fake adapters (no HTTP) and a temp-file SQLite store. Verifies:
- find_citations_batch writes to per-source tables and to citations
- find_citations_by_prefix uses prefix-capable adapters and enumerates the rest
- refresh_after skips fresh pairs and re-queries stale ones
- A second run preserves first_retrieved_at on existing rows
- find_citations(db_path=...) writes through and returns RunSummary
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pandas as pd
import pytest

from dataset_citation_finder import (
    RunSummary,
    describe_store,
    find_citations,
    find_citations_batch,
    find_citations_by_prefix,
    open_store,
    read_citations,
)
from dataset_citation_finder.adapters import REGISTRY
from dataset_citation_finder.adapters import base as base_module
from dataset_citation_finder.schema import LongRow


class FakeDataCite(base_module.SourceAdapter):
    source_id = "datacite_events"
    supports_prefix = True

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi, citing_doi="10.1038/cited",
            source=self.source_id, relation_type="is-cited-by",
            extra={"event_id": f"evt-{doi}", "upstream_source": "crossref"},
        )

    async def query_prefix(self, prefix: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=f"{prefix}/pasta/first", citing_doi="10.1038/pf-1",
            source=self.source_id, relation_type="is-cited-by",
            extra={"event_id": "evt-prefix-1"},
        )
        yield LongRow(
            cited_doi=f"{prefix}/pasta/second", citing_doi="10.1038/pf-2",
            source=self.source_id, relation_type="references",
            extra={"event_id": "evt-prefix-2"},
        )


class FakeOpenAlex(base_module.SourceAdapter):
    source_id = "openalex"

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi, citing_doi="10.1038/cited",
            source=self.source_id,
            extra={"openalex_id": "https://openalex.org/W1", "title": "Shared paper"},
        )


class FakeOpenCitations(base_module.SourceAdapter):
    source_id = "opencitations"
    default_concurrency = 4

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi, citing_doi="10.1038/oc-cited",
            source=self.source_id,
            extra={"creation": "2024-01-01", "journal_sc": "no"},
        )


class FakePrefixEnumerator:
    def __init__(self, *args, **kwargs): pass
    async def enumerate(self, prefix: str) -> list[str]:
        return [f"{prefix}/pasta/enum1", f"{prefix}/pasta/enum2"]


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setitem(REGISTRY, "datacite_events", FakeDataCite)
    monkeypatch.setitem(REGISTRY, "openalex", FakeOpenAlex)
    monkeypatch.setitem(REGISTRY, "opencitations", FakeOpenCitations)
    # Stub the still-NotImplemented adapters so the default source list works.
    class NoOp(base_module.SourceAdapter):
        source_id = "noop"
        async def query_single(self, doi):
            if False: yield  # noqa
    for sid in ("semantic_scholar", "crossref"):
        monkeypatch.setitem(REGISTRY, sid, type(f"NoOp_{sid}", (NoOp,), {"source_id": sid}))


@pytest.fixture
def fake_enumerator(monkeypatch):
    monkeypatch.setattr("dataset_citation_finder.api.PrefixEnumerator",
                        FakePrefixEnumerator)


class TestFindCitationsBatchToStore:

    def test_writes_per_source_tables_and_returns_summary(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        summary = find_citations_batch(["10.6073/a", "10.6073/b"], db)
        assert isinstance(summary, RunSummary)
        assert summary.run_id == 1
        assert summary.inputs_total == 2
        assert summary.inputs_skipped == 0
        # 2 DOIs * 3 working adapters = 6 rows written.
        assert summary.total_rows_written == 6
        assert summary.coalesced is True

        conn = open_store(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM source_datacite_events").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM source_openalex").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM source_opencitations").fetchone()[0] == 2
            # citations table: distinct (cited, citing) edges = 2 * (datacite shares one
            # citing_doi with openalex, opencitations has its own) = 2 datasets * 2 = 4.
            assert conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 4
        finally:
            conn.close()

    def test_run_row_recorded(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        conn = open_store(db)
        try:
            row = conn.execute(
                "SELECT input_kind, input_count, sources_succeeded, finished_at "
                "FROM runs"
            ).fetchone()
            assert row["input_kind"] == "batch"
            assert row["input_count"] == 1
            assert "openalex" in row["sources_succeeded"]
            assert row["finished_at"]
        finally:
            conn.close()


class TestSecondRunPreservesFirstRetrievedAt:

    def test_second_run_advances_last_keeps_first(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        conn = open_store(db)
        try:
            first_run = conn.execute(
                "SELECT first_retrieved_at, last_retrieved_at "
                "FROM source_openalex WHERE cited_doi='10.6073/a'"
            ).fetchone()
        finally:
            conn.close()

        # Re-run.
        find_citations_batch(["10.6073/a"], db, force=True)
        conn = open_store(db)
        try:
            second_run = conn.execute(
                "SELECT first_retrieved_at, last_retrieved_at "
                "FROM source_openalex WHERE cited_doi='10.6073/a'"
            ).fetchone()
        finally:
            conn.close()

        assert second_run["first_retrieved_at"] == first_run["first_retrieved_at"]
        assert second_run["last_retrieved_at"] >= first_run["last_retrieved_at"]


class TestRefreshAfterSkipsFreshPairs:

    def test_refresh_after_skips_recent(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a", "10.6073/b"], db)

        # Re-run with a long refresh window — everything is fresh.
        summary = find_citations_batch(
            ["10.6073/a", "10.6073/b"], db,
            refresh_after=timedelta(days=30),
        )
        # 3 working adapters * 2 DOIs each = 6 (cited_doi, source) skips.
        assert summary.inputs_skipped == 6
        assert summary.total_rows_written == 0

    def test_force_overrides_refresh_after(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        summary = find_citations_batch(
            ["10.6073/a"], db,
            refresh_after=timedelta(days=30),
            force=True,
        )
        assert summary.inputs_skipped == 0
        assert summary.total_rows_written == 3  # 1 DOI * 3 adapters


class TestFindCitationsByPrefixToStore:

    def test_prefix_path(self, fake_registry, fake_enumerator, tmp_path):
        db = tmp_path / "store.sqlite"
        summary = find_citations_by_prefix("10.6073", db)

        # 2 DataCite rows (prefix path) + 2 enum DOIs * 2 per-DOI adapters
        # (openalex, opencitations) = 2 + 4 = 6.
        assert summary.total_rows_written == 6

        conn = open_store(db)
        try:
            dce = conn.execute(
                "SELECT cited_doi FROM source_datacite_events ORDER BY cited_doi"
            ).fetchall()
            assert {r["cited_doi"] for r in dce} == {
                "10.6073/pasta/first", "10.6073/pasta/second",
            }
            # OpenAlex got the enumerated DOIs.
            ax = conn.execute(
                "SELECT DISTINCT cited_doi FROM source_openalex ORDER BY cited_doi"
            ).fetchall()
            assert {r["cited_doi"] for r in ax} == {
                "10.6073/pasta/enum1", "10.6073/pasta/enum2",
            }
        finally:
            conn.close()


class TestFindCitationsSingleWithDb:

    def test_single_with_db_path_returns_summary(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        summary = find_citations("10.6073/a", db_path=db)
        assert isinstance(summary, RunSummary)
        assert summary.total_rows_written == 3
        # And the in-memory return path still works without db_path.
        df = find_citations("10.6073/a")
        assert isinstance(df, pd.DataFrame)


class FlakyOpenAlex(base_module.SourceAdapter):
    """Completes every DOI except '…/c', on which it raises BudgetExhausted
    until `budget_ok` is flipped — simulating the daily cap and next-day reset.
    default_concurrency=1 makes processing order deterministic.
    """

    source_id = "openalex"
    default_concurrency = 1
    budget_ok = False

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        if not FlakyOpenAlex.budget_ok and doi.endswith("/c"):
            raise base_module.BudgetExhausted("daily cap")
        yield LongRow(
            cited_doi=doi, citing_doi="10.1038/oa-cited",
            source=self.source_id, extra={"openalex_id": "W1"},
        )


def _queried(db, source):
    conn = open_store(db)
    try:
        rows = conn.execute(
            "SELECT cited_doi FROM queried_dois WHERE source = ? ORDER BY cited_doi",
            (source,),
        ).fetchall()
        return {r["cited_doi"] for r in rows}
    finally:
        conn.close()


class TestBudgetExhaustedResume:
    """A daily-cap stop leaves un-finished DOIs out of queried_dois; a re-run
    (with refresh_after) resumes exactly those."""

    def test_stop_then_resume_next_run(self, monkeypatch, tmp_path):
        monkeypatch.setitem(REGISTRY, "openalex", FlakyOpenAlex)
        FlakyOpenAlex.budget_ok = False
        db = tmp_path / "store.sqlite"
        dois = ["10.6073/a", "10.6073/b", "10.6073/c", "10.6073/d"]

        # Run 1: cap hit on '…/c'. a, b complete; c (trigger) and d
        # (un-attempted) are left for next run.
        find_citations_batch(dois, db, sources=["openalex"])
        assert _queried(db, "openalex") == {"10.6073/a", "10.6073/b"}

        # Run 2 (next day): budget restored. refresh_after skips the two fresh
        # DOIs and re-queries only the un-bumped c and d.
        FlakyOpenAlex.budget_ok = True
        find_citations_batch(
            dois, db, sources=["openalex"], refresh_after=timedelta(days=1),
        )
        assert _queried(db, "openalex") == {
            "10.6073/a", "10.6073/b", "10.6073/c", "10.6073/d",
        }
        FlakyOpenAlex.budget_ok = False  # reset shared class state


class TestReadCitations:

    def test_coalesced_view(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        df = read_citations(db, view="coalesced")
        assert "sources" in df.columns
        # Two distinct edges expected: (a, cited) shared by datacite+openalex,
        # (a, oc-cited) for opencitations only.
        assert len(df) == 2

    def test_long_view(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        df = read_citations(db, view="long")
        assert set(df["source"]) == {"datacite_events", "openalex", "opencitations"}

    def test_per_source_view(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a"], db)
        df = read_citations(db, view="datacite_events")
        assert "event_id" in df.columns


class TestDescribeStoreAfterRun:

    def test_describe_reflects_run(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(["10.6073/a", "10.6073/b"], db)
        info = describe_store(db)
        assert info.run_count == 1
        assert info.rows_per_source["openalex"] == 2
        assert info.citations_count == 4
        assert info.distinct_cited_dois == 2
