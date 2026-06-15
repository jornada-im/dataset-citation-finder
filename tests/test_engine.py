"""Tests for engine.collect_rows, rows_to_dataframe, rows_to_grouped."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from dataset_citation_finder.adapters.base import SourceAdapter
from dataset_citation_finder.engine import (
    collect_rows,
    rows_to_dataframe,
    rows_to_grouped,
)
from dataset_citation_finder.schema import LongRow


class FakeAdapter(SourceAdapter):
    """Adapter that yields rows from a fixture without HTTP."""

    def __init__(self, source_id: str, fixture: dict, *, supports_prefix=False, raises=None):
        # Skip SourceAdapter.__init__ (no HTTP client needed).
        self.source_id = source_id
        self.supports_prefix = supports_prefix
        self.supports_batch = False
        self._fixture = fixture  # {"per_doi": {doi: [rows]}, "prefix": {prefix: [rows]}}
        self._raises = raises
        self._failed_dois: set[str] = set()
        import asyncio
        self._semaphore = asyncio.Semaphore(8)

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        if self._raises:
            raise self._raises
        for row_kwargs in self._fixture.get("per_doi", {}).get(doi, []):
            yield LongRow(**{**row_kwargs, "source": self.source_id})

    async def query_prefix(self, prefix: str) -> AsyncIterator[LongRow]:
        for row_kwargs in self._fixture.get("prefix", {}).get(prefix, []):
            yield LongRow(**{**row_kwargs, "source": self.source_id})


class TestCollectRows:

    @pytest.mark.asyncio
    async def test_per_doi_fanout(self):
        adapter = FakeAdapter("fake", {
            "per_doi": {
                "10.6073/a": [
                    {"cited_doi": "10.6073/a", "citing_doi": "10.1234/x"},
                    {"cited_doi": "10.6073/a", "citing_doi": "10.1234/y"},
                ],
                "10.6073/b": [
                    {"cited_doi": "10.6073/b", "citing_doi": "10.1234/z"},
                ],
            }
        })
        rows = await collect_rows([adapter], dois=["10.6073/a", "10.6073/b"])
        assert len(rows) == 3
        citing = {r.citing_doi for r in rows}
        assert citing == {"10.1234/x", "10.1234/y", "10.1234/z"}

    @pytest.mark.asyncio
    async def test_prefix_path_when_supported(self):
        adapter = FakeAdapter("fake", {
            "prefix": {
                "10.6073": [
                    {"cited_doi": "10.6073/foo", "citing_doi": "10.1234/a"},
                ]
            }
        }, supports_prefix=True)
        rows = await collect_rows([adapter], prefix="10.6073")
        assert len(rows) == 1
        assert rows[0].cited_doi == "10.6073/foo"

    @pytest.mark.asyncio
    async def test_prefix_path_skips_non_prefix_adapters(self):
        """Adapters without prefix support should be silently skipped when a
        prefix query is issued — the API layer is responsible for enumerating
        DOIs for them separately."""
        adapter = FakeAdapter("fake-no-prefix", {
            "per_doi": {"10.6073/foo": [{"cited_doi": "10.6073/foo", "citing_doi": "10.1234/a"}]}
        }, supports_prefix=False)
        rows = await collect_rows([adapter], prefix="10.6073")
        assert rows == []

    @pytest.mark.asyncio
    async def test_adapter_exception_is_isolated(self):
        """A broken adapter shouldn't take down the whole run."""
        good = FakeAdapter("good", {
            "per_doi": {"10.6073/a": [{"cited_doi": "10.6073/a", "citing_doi": "10.1234/x"}]}
        })
        bad = FakeAdapter("bad", {}, raises=RuntimeError("boom"))
        rows = await collect_rows([good, bad], dois=["10.6073/a"])
        assert len(rows) == 1
        assert rows[0].source == "good"

    @pytest.mark.asyncio
    async def test_requires_exactly_one_input(self):
        adapter = FakeAdapter("fake", {})
        with pytest.raises(ValueError):
            await collect_rows([adapter], dois=["10.1234/x"], prefix="10.1")
        with pytest.raises(ValueError):
            await collect_rows([adapter])


class TestRowsToDataFrame:

    def test_empty_input(self):
        df = rows_to_dataframe([])
        assert df.empty
        assert list(df.columns) == [
            "cited_doi", "citing_doi", "source", "relation_type", "retrieved_at"
        ]

    def test_basic_shape(self):
        rows = [
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="openalex"),
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="datacite_events",
                    relation_type="is-cited-by"),
        ]
        df = rows_to_dataframe(rows)
        assert len(df) == 2
        assert set(df["source"]) == {"openalex", "datacite_events"}


class TestRowsToGrouped:

    def test_merges_by_citing_doi(self):
        rows = [
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="openalex",
                    extra={"openalex_id": "https://openalex.org/W1", "title": "A paper"}),
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="datacite_events",
                    relation_type="is-cited-by", extra={"event_id": "evt1"}),
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="datacite_events",
                    relation_type="references", extra={"event_id": "evt2"}),
        ]
        grouped = rows_to_grouped(rows)
        assert len(grouped) == 1
        rec = grouped[0]
        assert rec.doi == "10.1234/x"
        assert set(rec.sources) == {"openalex", "datacite_events"}
        assert rec.title == "A paper"
        assert rec.openalex_id == "https://openalex.org/W1"
        assert set(rec.datacite_event_ids) == {"evt1", "evt2"}
        assert set(rec.datacite_relation_types) == {"is-cited-by", "references"}
        assert "10.6073/a" in rec.cited_dois

    def test_different_citing_dois_separate_records(self):
        rows = [
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="openalex"),
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/y", source="openalex"),
        ]
        grouped = rows_to_grouped(rows)
        assert len(grouped) == 2

    def test_same_citing_multiple_datasets(self):
        """One paper citing two datasets -> one grouped record with two cited_dois."""
        rows = [
            LongRow(cited_doi="10.6073/a", citing_doi="10.1234/x", source="openalex"),
            LongRow(cited_doi="10.6073/b", citing_doi="10.1234/x", source="openalex"),
        ]
        grouped = rows_to_grouped(rows)
        assert len(grouped) == 1
        assert set(grouped[0].cited_dois) == {"10.6073/a", "10.6073/b"}
