"""End-to-end tests for the in-memory single-DOI path.

Exercises find_citations (returns DataFrame / grouped) without a db_path.
SQLite-backed paths (find_citations_batch, find_citations_by_prefix, and
find_citations(db_path=...)) are covered in tests/test_store/test_api_to_store.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pandas as pd
import pytest

from dataset_citation_finder import (
    collapse_citations_df,
    find_citations,
    find_citations_batch,
)
from dataset_citation_finder.adapters import REGISTRY
from dataset_citation_finder.adapters import base as base_module
from dataset_citation_finder.schema import LongRow


class FakeDataCiteLike(base_module.SourceAdapter):
    source_id = "datacite_events"
    supports_prefix = True

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi,
            citing_doi="10.1038/nature-cited",
            source=self.source_id,
            relation_type="is-cited-by",
            extra={"event_id": f"evt-{doi}"},
        )

    async def query_prefix(self, prefix: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=f"{prefix}/pasta/first",
            citing_doi="10.1038/nature-first",
            source=self.source_id,
            relation_type="is-cited-by",
        )


class FakeOpenAlexLike(base_module.SourceAdapter):
    source_id = "openalex"

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi,
            citing_doi="10.1038/nature-cited",
            source=self.source_id,
            extra={"title": "Shared paper", "openalex_id": "W123"},
        )


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setitem(REGISTRY, "datacite_events", FakeDataCiteLike)
    monkeypatch.setitem(REGISTRY, "openalex", FakeOpenAlexLike)

    class NoOp(base_module.SourceAdapter):
        source_id = "noop"
        async def query_single(self, doi):
            if False:
                yield
    for sid in ("semantic_scholar", "crossref", "opencitations"):
        monkeypatch.setitem(REGISTRY, sid, type(f"NoOp_{sid}", (NoOp,), {"source_id": sid}))


class TestFindCitationsInMemory:

    def test_single_doi_default_long_output(self, fake_registry):
        df = find_citations("10.6073/pasta/abc")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert set(df["source"]) == {"datacite_events", "openalex"}
        assert (df["cited_doi"] == "10.6073/pasta/abc").all()

    def test_grouped_output(self, fake_registry):
        records = find_citations("10.6073/pasta/abc", output="grouped")
        assert isinstance(records, list)
        assert len(records) == 1
        rec = records[0]
        assert rec.doi == "10.1038/nature-cited"
        assert set(rec.sources) == {"datacite_events", "openalex"}
        assert rec.title == "Shared paper"
        assert rec.openalex_id == "W123"
        assert "is-cited-by" in rec.datacite_relation_types

    def test_invalid_doi_raises(self, fake_registry):
        with pytest.raises(ValueError, match="invalid DOI"):
            find_citations("not-a-doi")

    def test_source_subset(self, fake_registry):
        df = find_citations("10.6073/pasta/abc", sources=["openalex"])
        assert set(df["source"]) == {"openalex"}
        assert len(df) == 1


class TestFindCitationsBatchValidation:
    """The DOI-validation branches don't depend on a working store."""

    def test_all_invalid_raises(self, fake_registry, tmp_path):
        with pytest.raises(ValueError, match="no valid DOIs"):
            find_citations_batch(["not", "also-not"], tmp_path / "x.sqlite")


class TestFindCitationsBatchInMemory:
    """find_citations_batch with no db_path runs the in-memory pipeline."""

    def test_returns_dataframe_when_no_db(self, fake_registry):
        df = find_citations_batch(["10.6073/pasta/a", "10.6073/pasta/b"])
        assert isinstance(df, pd.DataFrame)
        # 2 DOIs * 2 working adapters = 4 rows.
        assert len(df) == 4
        assert set(df["cited_doi"]) == {"10.6073/pasta/a", "10.6073/pasta/b"}

    def test_grouped_output_when_no_db(self, fake_registry):
        records = find_citations_batch(
            ["10.6073/pasta/a"], output="grouped"
        )
        assert isinstance(records, list)
        assert len(records) == 1
        assert records[0].doi == "10.1038/nature-cited"


class TestCollapseSourcesReExport:

    def test_available_at_top_level(self, fake_registry):
        df = find_citations("10.6073/pasta/abc")
        collapsed = collapse_citations_df(df)
        assert len(collapsed) == 1
        assert set(collapsed.iloc[0]["sources"]) == {"datacite_events", "openalex"}
        assert collapsed.iloc[0]["source_count"] == 2
