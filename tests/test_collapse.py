"""Tests for collapse.collapse_sources()."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from dataset_citation_finder.collapse import collapse_sources


def _row(cited, citing, source, relation=None, ts=None):
    return {
        "cited_doi": cited,
        "citing_doi": citing,
        "source": source,
        "relation_type": relation,
        "retrieved_at": ts or datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc),
    }


class TestCollapseSources:

    def test_empty_input_returns_empty_frame(self):
        df = pd.DataFrame(columns=[
            "cited_doi", "citing_doi", "source", "relation_type", "retrieved_at"
        ])
        out = collapse_sources(df)
        assert out.empty
        assert set(out.columns) == {
            "cited_doi", "citing_doi", "sources", "source_count",
            "relation_types", "first_retrieved_at",
        }

    def test_single_source_per_edge(self):
        df = pd.DataFrame([
            _row("10.6073/a", "10.1038/b", "openalex"),
            _row("10.6073/a", "10.1038/c", "openalex"),
        ])
        out = collapse_sources(df)
        assert len(out) == 2
        for _, r in out.iterrows():
            assert r["sources"] == ["openalex"]
            assert r["source_count"] == 1

    def test_cross_source_dedup(self):
        """Same edge reported by three sources collapses to one row with three sources."""
        df = pd.DataFrame([
            _row("10.6073/a", "10.1038/b", "openalex"),
            _row("10.6073/a", "10.1038/b", "datacite_events", relation="is-cited-by"),
            _row("10.6073/a", "10.1038/b", "opencitations"),
        ])
        out = collapse_sources(df)
        assert len(out) == 1
        row = out.iloc[0]
        assert set(row["sources"]) == {"openalex", "datacite_events", "opencitations"}
        assert row["source_count"] == 3
        assert "is-cited-by" in row["relation_types"]

    def test_relation_types_deduplicated(self):
        """Two DataCite events for the same edge with different relation types -> merged list."""
        df = pd.DataFrame([
            _row("10.6073/a", "10.1038/b", "datacite_events", relation="is-cited-by"),
            _row("10.6073/a", "10.1038/b", "datacite_events", relation="references"),
        ])
        out = collapse_sources(df)
        assert len(out) == 1
        assert set(out.iloc[0]["relation_types"]) == {"is-cited-by", "references"}

    def test_first_retrieved_at_is_minimum(self):
        early = datetime(2026, 1, 1, tzinfo=timezone.utc)
        late = datetime(2026, 6, 1, tzinfo=timezone.utc)
        df = pd.DataFrame([
            _row("10.6073/a", "10.1038/b", "openalex", ts=late),
            _row("10.6073/a", "10.1038/b", "datacite_events", ts=early),
        ])
        out = collapse_sources(df)
        assert out.iloc[0]["first_retrieved_at"] == early

    def test_missing_columns_raises(self):
        df = pd.DataFrame([{"cited_doi": "10.1/x"}])
        with pytest.raises(ValueError, match="missing required columns"):
            collapse_sources(df)

    def test_different_cited_dois_not_merged(self):
        """Same citing_doi citing two different datasets -> two rows."""
        df = pd.DataFrame([
            _row("10.6073/a", "10.1038/b", "openalex"),
            _row("10.6073/x", "10.1038/b", "openalex"),
        ])
        out = collapse_sources(df)
        assert len(out) == 2
