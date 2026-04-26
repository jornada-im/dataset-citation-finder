"""Tests for schema.LongRow and schema.GroupedRecord."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from dataset_citation_finder.schema import GroupedRecord, LongRow


class TestLongRow:

    def test_minimal_row(self):
        row = LongRow(
            cited_doi="10.6073/pasta/abc",
            citing_doi="10.1038/xyz",
            source="datacite_events",
        )
        assert row.cited_doi == "10.6073/pasta/abc"
        assert row.citing_doi == "10.1038/xyz"
        assert row.source == "datacite_events"
        assert row.relation_type is None
        assert isinstance(row.retrieved_at, datetime)
        assert row.retrieved_at.tzinfo == timezone.utc

    def test_normalizes_dois_on_construction(self):
        row = LongRow(
            cited_doi="https://doi.org/10.6073/PASTA/ABC",
            citing_doi="DOI:10.1038/XYZ",
            source="openalex",
        )
        assert row.cited_doi == "10.6073/pasta/abc"
        assert row.citing_doi == "10.1038/xyz"

    def test_rejects_invalid_doi(self):
        with pytest.raises(ValidationError):
            LongRow(cited_doi="not-a-doi", citing_doi="10.1038/xyz", source="x")

    def test_extra_metadata(self):
        row = LongRow(
            cited_doi="10.6073/pasta/abc",
            citing_doi="10.1038/xyz",
            source="datacite_events",
            relation_type="is-cited-by",
            extra={"event_id": "foo", "upstream_source": "crossref"},
        )
        assert row.relation_type == "is-cited-by"
        assert row.extra["event_id"] == "foo"


class TestGroupedRecord:

    def test_empty_is_valid(self):
        rec = GroupedRecord()
        assert rec.doi is None
        assert rec.sources == []
        assert rec.datacite_relation_types == []

    def test_doi_normalized(self):
        rec = GroupedRecord(doi="HTTPS://DOI.ORG/10.1038/XYZ")
        assert rec.doi == "10.1038/xyz"

    def test_preserves_provenance_fields(self):
        rec = GroupedRecord(
            doi="10.1038/xyz",
            sources=["datacite_events", "openalex"],
            datacite_relation_types=["is-cited-by", "references"],
            openalex_id="https://openalex.org/W123",
        )
        assert rec.sources == ["datacite_events", "openalex"]
        assert "references" in rec.datacite_relation_types
        assert rec.openalex_id == "https://openalex.org/W123"
