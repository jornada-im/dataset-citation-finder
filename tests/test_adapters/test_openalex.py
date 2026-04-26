"""Tests for OpenAlexAdapter.

OpenAlex resolves DOI -> work ID, then queries works that cite it.
"""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.adapters.openalex import OpenAlexAdapter


class TestOpenAlexAdapter:

    @pytest.mark.asyncio
    async def test_resolves_doi_and_lists_citers(self, make_mock_client):
        def handler(request):
            if request.url.path.startswith("/works/doi:"):
                # DOI resolution call.
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999",
                    "doi": "https://doi.org/10.6073/pasta/abc",
                })
            # Listing citers.
            assert request.url.params.get("filter") == "cites:W999"
            return httpx.Response(200, json={
                "results": [
                    {"id": "https://openalex.org/W1",
                     "doi": "https://doi.org/10.1038/nature1",
                     "title": "Paper one", "publication_year": 2023},
                    {"id": "https://openalex.org/W2",
                     "doi": "https://doi.org/10.1126/science.xyz",
                     "title": "Paper two", "publication_year": 2024},
                ],
                "meta": {"next_cursor": None},
            })

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        citing = {r.citing_doi for r in rows}
        assert citing == {"10.1038/nature1", "10.1126/science.xyz"}
        assert all(r.source == "openalex" for r in rows)
        assert all(r.cited_doi == "10.6073/pasta/abc" for r in rows)
        # Titles / years captured in extras.
        titles = {r.extra.get("title") for r in rows}
        assert titles == {"Paper one", "Paper two"}
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unresolvable_doi_yields_nothing(self, make_mock_client):
        def handler(request):
            return httpx.Response(404, json={"error": "not found"})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/nonexistent")]
        assert rows == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, make_mock_client):
        call_count = [0]

        def handler(request):
            if request.url.path.startswith("/works/doi:"):
                return httpx.Response(200, json={"id": "https://openalex.org/W999"})
            call_count[0] += 1
            cursor = request.url.params.get("cursor")
            if cursor == "*":
                return httpx.Response(200, json={
                    "results": [{"id": "https://openalex.org/W1",
                                 "doi": "https://doi.org/10.1038/a"}],
                    "meta": {"next_cursor": "page2"},
                })
            if cursor == "page2":
                return httpx.Response(200, json={
                    "results": [{"id": "https://openalex.org/W2",
                                 "doi": "https://doi.org/10.1038/b"}],
                    "meta": {"next_cursor": None},
                })
            return httpx.Response(500)

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        assert call_count[0] == 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_skips_citers_without_doi(self, make_mock_client):
        def handler(request):
            if request.url.path.startswith("/works/doi:"):
                return httpx.Response(200, json={"id": "https://openalex.org/W999"})
            return httpx.Response(200, json={
                "results": [
                    {"id": "https://openalex.org/W1", "doi": None, "title": "No DOI"},
                    {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1038/b"},
                ],
                "meta": {"next_cursor": None},
            })

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].citing_doi == "10.1038/b"
        await client.aclose()
