"""Tests for OpenAlexAdapter.

The adapter resolves each DOI via the per-DOI endpoint `/works/doi:{doi}`
(asking only for id + cited_by_count), prunes datasets with
`cited_by_count == 0`, then pages through the `cites:` listing for the rest.
"""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.adapters.base import BudgetExhausted
from dataset_citation_finder.adapters.openalex import OpenAlexAdapter


def _is_resolve(request) -> bool:
    return request.url.path.startswith("/works/doi:")


def _filter(request) -> str:
    return request.url.params.get("filter", "")


class TestOpenAlexAdapter:

    @pytest.mark.asyncio
    async def test_resolves_doi_and_lists_citers(self, make_mock_client):
        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 2,
                })
            assert _filter(request) == "cites:W999"
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
        assert {r.citing_doi for r in rows} == {"10.1038/nature1", "10.1126/science.xyz"}
        assert all(r.source == "openalex" for r in rows)
        assert all(r.cited_doi == "10.6073/pasta/abc" for r in rows)
        assert {r.extra.get("title") for r in rows} == {"Paper one", "Paper two"}
        # cited side is enriched with the dataset's own OpenAlex id.
        assert all(r.extra.get("cited_openalex_id") == "https://openalex.org/W999" for r in rows)
        assert adapter.take_failed_dois() == set()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unresolvable_doi_yields_nothing_and_is_not_failed(self, make_mock_client):
        def handler(request):
            return httpx.Response(404, json={"error": "not found"})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/nonexistent")]
        assert rows == []
        assert adapter.take_failed_dois() == set()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_zero_cited_by_count_skips_listing(self, make_mock_client):
        listing_calls = [0]

        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 0,
                })
            listing_calls[0] += 1
            return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert rows == []
        assert listing_calls[0] == 0  # no citers listing issued
        assert adapter.take_failed_dois() == set()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, make_mock_client):
        listing_calls = [0]

        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 2,
                })
            listing_calls[0] += 1
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
            return httpx.Response(400)

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        assert listing_calls[0] == 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_skips_citers_without_doi(self, make_mock_client):
        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 2,
                })
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

    @pytest.mark.asyncio
    async def test_resolve_error_marks_doi_failed(self, make_mock_client):
        # 403 is non-retryable, so the failure surfaces immediately (the
        # backoff-retry path is covered by the http_client tests).
        def handler(request):
            return httpx.Response(403)

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert rows == []
        assert adapter.take_failed_dois() == {"10.6073/pasta/abc"}
        await client.aclose()

    @pytest.mark.asyncio
    async def test_listing_error_marks_doi_failed(self, make_mock_client):
        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 2,
                })
            return httpx.Response(403)

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert rows == []
        assert adapter.take_failed_dois() == {"10.6073/pasta/abc"}
        await client.aclose()

    @pytest.mark.asyncio
    async def test_429_on_resolve_raises_budget_exhausted(self, make_mock_client):
        calls = [0]

        def handler(request):
            calls[0] += 1
            return httpx.Response(429, headers={"X-RateLimit-Reset": "43200"})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        with pytest.raises(BudgetExhausted):
            _ = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        # 429 is dropped from the retry set, so it surfaces on the first call.
        assert calls[0] == 1
        await client.aclose()

    @pytest.mark.asyncio
    async def test_429_on_listing_raises_budget_exhausted(self, make_mock_client):
        def handler(request):
            if _is_resolve(request):
                return httpx.Response(200, json={
                    "id": "https://openalex.org/W999", "cited_by_count": 2,
                })
            return httpx.Response(429, headers={"X-RateLimit-Reset": "100"})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)
        with pytest.raises(BudgetExhausted):
            _ = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        await client.aclose()
