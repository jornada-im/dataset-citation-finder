"""Tests for the cited_dois table and its upsert helper.

Covers: insertion, conflict preserves first_seen_at and origin, populated
automatically by find_citations_batch when db_path is given, surfaces
uncited DOIs via read_citations(view='cited_dois').
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from dataset_citation_finder import (
    find_citations_batch,
    open_store,
    read_citations,
)
from dataset_citation_finder.adapters import REGISTRY
from dataset_citation_finder.adapters import base as base_module
from dataset_citation_finder.schema import LongRow
from dataset_citation_finder.store.upsert import insert_run, upsert_cited_dois


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "store.sqlite"
    c = open_store(db)
    yield c
    c.close()


class TestCitedDoisUpsert:

    def test_insert_then_reinsert_preserves_first_seen_and_origin(self, conn):
        run1 = insert_run(conn, input_kind="batch", input_value=None,
                          input_count=1, sources_requested=["openalex"])
        upsert_cited_dois(
            conn, cited_dois=["10.1234/a"], origin="input", run_id=run1,
        )
        first = conn.execute(
            "SELECT first_seen_at, last_seen_at, origin FROM cited_dois "
            "WHERE cited_doi = '10.1234/a'"
        ).fetchone()

        time.sleep(0.01)
        run2 = insert_run(conn, input_kind="batch", input_value=None,
                          input_count=1, sources_requested=["openalex"])
        # Re-insert under a different origin — must NOT overwrite the original.
        upsert_cited_dois(
            conn, cited_dois=["10.1234/a"], origin="prefix:10.1234", run_id=run2,
        )
        second = conn.execute(
            "SELECT first_seen_at, last_seen_at, origin, "
            "first_seen_run_id, last_seen_run_id FROM cited_dois "
            "WHERE cited_doi = '10.1234/a'"
        ).fetchone()

        assert second["first_seen_at"] == first["first_seen_at"]
        assert second["origin"] == first["origin"]  # preserved
        assert second["first_seen_run_id"] == run1
        assert second["last_seen_at"] >= first["last_seen_at"]
        assert second["last_seen_run_id"] == run2

    def test_validation_columns_default_null(self, conn):
        run = insert_run(conn, input_kind="batch", input_value=None,
                         input_count=1, sources_requested=["openalex"])
        upsert_cited_dois(
            conn, cited_dois=["10.1234/a"], origin="input", run_id=run,
        )
        row = conn.execute(
            "SELECT validation_status, validated_at, validated_registrar "
            "FROM cited_dois WHERE cited_doi = '10.1234/a'"
        ).fetchone()
        assert row["validation_status"] is None
        assert row["validated_at"] is None
        assert row["validated_registrar"] is None


# ---- end-to-end: find_citations_batch populates cited_dois ----------------

class FakeOpenAlex(base_module.SourceAdapter):
    source_id = "openalex"

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        # Only return rows for DOIs whose path starts with "has-"; the
        # "no-" DOIs return nothing (simulating uncited dataset DOIs).
        if "/has-" in doi:
            yield LongRow(
                cited_doi=doi, citing_doi="10.1038/x", source=self.source_id,
                extra={"openalex_id": "W1"},
            )


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setitem(REGISTRY, "openalex", FakeOpenAlex)

    class NoOp(base_module.SourceAdapter):
        source_id = "noop"
        async def query_single(self, doi):
            if False:
                yield
    for sid in ("datacite_events", "semantic_scholar", "crossref", "opencitations"):
        monkeypatch.setitem(REGISTRY, sid, type(f"NoOp_{sid}", (NoOp,), {"source_id": sid}))


class TestCitedDoisFromBatch:

    def test_uncited_dois_recorded_in_cited_dois(self, fake_registry, tmp_path):
        db = tmp_path / "store.sqlite"
        find_citations_batch(
            ["10.6073/has-a", "10.6073/no-b", "10.6073/no-c"],
            db_path=db,
        )

        df = read_citations(db, view="cited_dois")
        assert set(df["cited_doi"]) == {
            "10.6073/has-a", "10.6073/no-b", "10.6073/no-c"
        }
        # All recorded with origin='input'.
        assert (df["origin"] == "input").all()

    def test_left_join_finds_uncited(self, fake_registry, tmp_path):
        """The motivating use case: which DOIs in our scope have zero citations?"""
        db = tmp_path / "store.sqlite"
        find_citations_batch(
            ["10.6073/has-a", "10.6073/no-b"],
            db_path=db,
        )
        conn = open_store(db, create=False)
        try:
            uncited = conn.execute(
                "SELECT cd.cited_doi FROM cited_dois cd "
                "LEFT JOIN citations c ON c.cited_doi = cd.cited_doi "
                "WHERE c.cited_doi IS NULL "
                "ORDER BY cd.cited_doi"
            ).fetchall()
        finally:
            conn.close()
        assert [r["cited_doi"] for r in uncited] == ["10.6073/no-b"]
