"""End-to-end tests for the public API.

These exercise find_citations, find_citations_batch, find_citations_by_shoulder
by monkey-patching the REGISTRY with a fake adapter, so no HTTP happens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pandas as pd
import pytest

from dataset_citation_finder import (
    collapse_sources,
    find_citations,
    find_citations_batch,
    find_citations_by_shoulder,
)
from dataset_citation_finder.adapters import base as base_module
from dataset_citation_finder.adapters import REGISTRY
from dataset_citation_finder.schema import LongRow


class FakeDataCiteLike(base_module.SourceAdapter):
    """Simulates DataCite Events: supports prefix and per-DOI queries."""

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
        yield LongRow(
            cited_doi=f"{prefix}/pasta/second",
            citing_doi="10.1038/nature-second",
            source=self.source_id,
            relation_type="references",
        )


class FakeOpenAlexLike(base_module.SourceAdapter):
    """Simulates OpenAlex: per-DOI only."""

    source_id = "openalex"

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        yield LongRow(
            cited_doi=doi,
            citing_doi="10.1038/nature-cited",  # overlaps with DataCite
            source=self.source_id,
            extra={"title": "Shared paper", "openalex_id": "W123"},
        )


class FakeShoulderEnumerator:
    """Stand-in for the real ShoulderEnumerator — returns a fixed list."""

    def __init__(self, *args, **kwargs):
        pass

    async def enumerate(self, prefix: str) -> list[str]:
        return [f"{prefix}/pasta/enum1", f"{prefix}/pasta/enum2"]


@pytest.fixture
def fake_registry(monkeypatch):
    """Swap the adapter registry for fakes. Also neutralize stubs that would
    raise NotImplementedError."""
    monkeypatch.setitem(REGISTRY, "datacite_events", FakeDataCiteLike)
    monkeypatch.setitem(REGISTRY, "openalex", FakeOpenAlexLike)
    # Point semantic_scholar / crossref / opencitations at a no-op so they
    # don't raise NotImplementedError during the default-sources path.
    class NoOp(base_module.SourceAdapter):
        source_id = "noop"
        async def query_single(self, doi):
            if False:
                yield
    for sid in ("semantic_scholar", "crossref", "opencitations"):
        noop_cls = type(f"NoOp_{sid}", (NoOp,), {"source_id": sid})
        monkeypatch.setitem(REGISTRY, sid, noop_cls)


@pytest.fixture
def fake_enumerator(monkeypatch):
    """Replace the real ShoulderEnumerator with a fixture-backed one."""
    monkeypatch.setattr(
        "dataset_citation_finder.api.ShoulderEnumerator",
        FakeShoulderEnumerator,
    )


class TestFindCitations:

    def test_single_doi_default_long_output(self, fake_registry):
        df = find_citations("10.6073/pasta/abc")
        assert isinstance(df, pd.DataFrame)
        # DataCite + OpenAlex each return 1 row = 2 rows total.
        assert len(df) == 2
        assert set(df["source"]) == {"datacite_events", "openalex"}
        assert (df["cited_doi"] == "10.6073/pasta/abc").all()

    def test_grouped_output(self, fake_registry):
        records = find_citations("10.6073/pasta/abc", output="grouped")
        assert isinstance(records, list)
        # Both sources report the same citing_doi -> 1 merged record.
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


class TestFindCitationsBatch:

    def test_batch_fans_out(self, fake_registry):
        df = find_citations_batch(["10.6073/pasta/a", "10.6073/pasta/b"])
        # Each DOI -> 2 rows (datacite + openalex). 2 DOIs x 2 sources = 4.
        assert len(df) == 4
        assert set(df["cited_doi"]) == {"10.6073/pasta/a", "10.6073/pasta/b"}

    def test_skips_invalid_dois(self, fake_registry):
        df = find_citations_batch(["10.6073/pasta/a", "not-a-doi", "10.6073/pasta/b"])
        # Should still get 4 rows for the 2 valid DOIs.
        assert len(df) == 4

    def test_all_invalid_raises(self, fake_registry):
        with pytest.raises(ValueError, match="no valid DOIs"):
            find_citations_batch(["not", "also-not"])


class TestFindCitationsByShoulder:

    def test_prefix_capable_uses_prefix_query(self, fake_registry, fake_enumerator):
        df = find_citations_by_shoulder("10.6073")
        # DataCite returns 2 rows via query_prefix;
        # OpenAlex + noops (3 of them, each zero) run query_batch over 2 enumerated DOIs.
        # OpenAlex returns 1 row per enumerated DOI = 2 rows.
        # Total = 4.
        assert len(df) == 4
        datacite_rows = df[df["source"] == "datacite_events"]
        openalex_rows = df[df["source"] == "openalex"]
        assert len(datacite_rows) == 2
        assert len(openalex_rows) == 2
        # DataCite ones came from the prefix path — cited DOIs are the ones
        # the fake prefix query yielded.
        assert set(datacite_rows["cited_doi"]) == {
            "10.6073/pasta/first", "10.6073/pasta/second"
        }
        # OpenAlex ones came from enumerator.
        assert set(openalex_rows["cited_doi"]) == {
            "10.6073/pasta/enum1", "10.6073/pasta/enum2"
        }


class TestCollapseSourcesReExport:

    def test_available_at_top_level(self, fake_registry):
        """collapse_sources should round-trip with API output."""
        df = find_citations("10.6073/pasta/abc")
        collapsed = collapse_sources(df)
        assert len(collapsed) == 1  # Same citing_doi from two sources.
        assert set(collapsed.iloc[0]["sources"]) == {"datacite_events", "openalex"}
        assert collapsed.iloc[0]["source_count"] == 2
