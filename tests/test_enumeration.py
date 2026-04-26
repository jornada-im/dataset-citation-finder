"""Tests for PrefixEnumerator."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from dataset_citation_finder.enumeration import PrefixEnumerator


def _dois_page(dois, next_url=None):
    return {
        "data": [{"id": d, "attributes": {"doi": d}} for d in dois],
        "links": {"next": next_url} if next_url else {},
    }


class TestPrefixEnumerator:

    @pytest.mark.asyncio
    async def test_single_page(self, tmp_path):
        def handler(request):
            assert request.url.params.get("query") == "prefix:10.6073"
            return httpx.Response(200, json=_dois_page([
                "10.6073/pasta/a", "10.6073/pasta/b",
            ]))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        enum = PrefixEnumerator(client, cache_dir=tmp_path)
        dois = await enum.enumerate("10.6073")
        assert sorted(dois) == ["10.6073/pasta/a", "10.6073/pasta/b"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_paginated(self, tmp_path):
        def handler(request):
            cursor = request.url.params.get("page[cursor]")
            if cursor == "1":
                return httpx.Response(200, json=_dois_page(
                    ["10.6073/pasta/a"],
                    next_url="https://api.datacite.org/dois?page%5Bcursor%5D=2",
                ))
            if cursor == "2":
                return httpx.Response(200, json=_dois_page(["10.6073/pasta/b"]))
            return httpx.Response(500)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        enum = PrefixEnumerator(client, cache_dir=tmp_path)
        dois = await enum.enumerate("10.6073")
        assert sorted(dois) == ["10.6073/pasta/a", "10.6073/pasta/b"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_http(self, tmp_path):
        call_count = [0]

        def handler(request):
            call_count[0] += 1
            return httpx.Response(200, json=_dois_page(["10.6073/pasta/a"]))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        enum = PrefixEnumerator(client, cache_dir=tmp_path)

        dois1 = await enum.enumerate("10.6073")
        assert call_count[0] == 1
        dois2 = await enum.enumerate("10.6073")
        assert call_count[0] == 1  # No additional HTTP call.
        assert dois1 == dois2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_cache_expires(self, tmp_path):
        def handler(request):
            return httpx.Response(200, json=_dois_page(["10.6073/pasta/a"]))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        enum = PrefixEnumerator(client, cache_dir=tmp_path, ttl_seconds=0)
        await enum.enumerate("10.6073")

        # Cache file exists but is already stale (ttl=0).
        cache_path = tmp_path / "prefix_10.6073.json"
        assert cache_path.exists()
        payload = json.loads(cache_path.read_text())
        # Force timestamp far in the past.
        payload["ts"] = time.time() - 1000
        cache_path.write_text(json.dumps(payload))

        # Should re-fetch.
        new_client_calls = [0]

        def handler2(request):
            new_client_calls[0] += 1
            return httpx.Response(200, json=_dois_page(["10.6073/pasta/a"]))

        client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler2),
                                    base_url="http://mocked")
        enum2 = PrefixEnumerator(client2, cache_dir=tmp_path, ttl_seconds=0)
        await enum2.enumerate("10.6073")
        assert new_client_calls[0] >= 1
        await client.aclose()
        await client2.aclose()

    @pytest.mark.asyncio
    async def test_no_cache_dir_still_works(self):
        """Should run fine without caching when cache_dir is None."""
        def handler(request):
            return httpx.Response(200, json=_dois_page(["10.6073/pasta/a"]))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        enum = PrefixEnumerator(client, cache_dir=None)
        dois = await enum.enumerate("10.6073")
        assert dois == ["10.6073/pasta/a"]
        await client.aclose()
