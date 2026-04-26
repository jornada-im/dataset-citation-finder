"""Tests for OpenCitationsAdapter."""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.adapters.base import SourceAdapter
from dataset_citation_finder.adapters.opencitations import OpenCitationsAdapter


class TestOpenCitationsRateLimitAttributes:

    def test_concurrency_lower_than_default(self):
        assert OpenCitationsAdapter.default_concurrency < SourceAdapter.default_concurrency

    def test_inter_request_delay_set(self):
        assert OpenCitationsAdapter.inter_request_delay > 0


class TestOpenCitationsAdapter:

    @pytest.mark.asyncio
    async def test_basic_parse(self, make_mock_client):
        def handler(request):
            assert request.url.path.endswith("/citations/doi:10.6073/pasta/abc")
            return httpx.Response(200, json=[
                {"citing": "doi:10.1038/nature12345",
                 "cited": "doi:10.6073/pasta/abc",
                 "creation": "2023-05-01",
                 "journal_sc": "no",
                 "author_sc": "no",
                 "oci": "020-1234"},
                {"citing": "doi:10.1126/science.abc",
                 "cited": "doi:10.6073/pasta/abc",
                 "creation": "2024-01-01",
                 "journal_sc": "no",
                 "author_sc": "no",
                 "oci": "020-5678"},
            ])

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        assert all(r.source == "opencitations" for r in rows)
        assert all(r.cited_doi == "10.6073/pasta/abc" for r in rows)
        citing = {r.citing_doi for r in rows}
        assert citing == {"10.1038/nature12345", "10.1126/science.abc"}
        await client.aclose()

    @pytest.mark.asyncio
    async def test_skips_non_doi_citations(self, make_mock_client):
        """OpenCitations sometimes returns pmid: or pmcid: prefixes — we skip them."""
        def handler(request):
            return httpx.Response(200, json=[
                {"citing": "pmid:12345", "cited": "doi:10.6073/pasta/abc"},
                {"citing": "doi:10.1038/nature123", "cited": "doi:10.6073/pasta/abc"},
            ])

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].citing_doi == "10.1038/nature123"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_404_returns_empty(self, make_mock_client):
        def handler(request):
            return httpx.Response(404, json={"error": "not found"})

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert rows == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_extra_fields_preserved(self, make_mock_client):
        def handler(request):
            return httpx.Response(200, json=[
                {"citing": "doi:10.1038/nature123", "cited": "doi:10.6073/pasta/abc",
                 "creation": "2023-05-01", "journal_sc": "yes",
                 "author_sc": "no", "oci": "020-xyz"},
            ])

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].extra["journal_sc"] == "yes"
        assert rows[0].extra["oci"] == "020-xyz"
        await client.aclose()
