"""Tests for http_client: RetryPolicy, request_with_retry."""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.http_client import (
    RetryPolicy,
    make_client,
    request_with_retry,
)


class TestRetryPolicy:

    def test_delay_grows_exponentially(self):
        # With jitter in [0.5x, 1.0x], the second attempt should be strictly
        # larger than 0.5 * base * 2 and bounded by base * 2.
        p = RetryPolicy(base_delay=2.0, max_delay=100.0)
        # Run many iterations to check bounds.
        for _ in range(20):
            d1 = p.delay_for(1)  # in [1.0, 2.0]
            d2 = p.delay_for(2)  # in [2.0, 4.0]
            assert 1.0 <= d1 <= 2.0
            assert 2.0 <= d2 <= 4.0

    def test_delay_respects_max(self):
        p = RetryPolicy(base_delay=1.0, max_delay=5.0)
        # Large attempt number shouldn't exceed max.
        d = p.delay_for(20)
        assert d <= 5.0


class TestRequestWithRetry:

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self):
        call_count = [0]

        def handler(request):
            call_count[0] += 1
            if call_count[0] < 3:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        policy = RetryPolicy(max_attempts=5, base_delay=0.01, max_delay=0.05)
        resp = await request_with_retry(client, "GET", "http://mocked/foo", retry=policy)
        assert resp.status_code == 200
        assert call_count[0] == 3
        await client.aclose()

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self):
        call_count = [0]

        def handler(request):
            call_count[0] += 1
            return httpx.Response(503)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        policy = RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.05)
        resp = await request_with_retry(client, "GET", "http://mocked/foo", retry=policy)
        # Returns the final (failed) response rather than raising.
        assert resp.status_code == 503
        assert call_count[0] == 3
        await client.aclose()

    @pytest.mark.asyncio
    async def test_does_not_retry_on_404(self):
        call_count = [0]

        def handler(request):
            call_count[0] += 1
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mocked")
        policy = RetryPolicy(max_attempts=5, base_delay=0.01)
        resp = await request_with_retry(client, "GET", "http://mocked/foo", retry=policy)
        assert resp.status_code == 404
        assert call_count[0] == 1  # No retry on 404.
        await client.aclose()


class TestMakeClient:

    @pytest.mark.asyncio
    async def test_includes_email_in_user_agent(self):
        seen_ua = [None]

        def handler(request):
            seen_ua[0] = request.headers.get("User-Agent")
            return httpx.Response(200, json={})

        client = make_client(email="me@example.org")
        # Swap in mock transport.
        client._transport = httpx.MockTransport(handler)
        await client.get("http://example.com/x")
        assert "mailto:me@example.org" in seen_ua[0]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_email_still_sets_user_agent(self):
        seen_ua = [None]

        def handler(request):
            seen_ua[0] = request.headers.get("User-Agent")
            return httpx.Response(200, json={})

        client = make_client()
        client._transport = httpx.MockTransport(handler)
        await client.get("http://example.com/x")
        assert "dataset-citation-finder" in seen_ua[0]
        await client.aclose()
