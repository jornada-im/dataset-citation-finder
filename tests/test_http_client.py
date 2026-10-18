"""Tests for http_client.get retries and make_client headers."""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.http_client import MAX_ATTEMPTS, get, make_client


def _counting(responses):
    """Handler returning `responses` in order; records how many were used."""
    calls = []

    def handler(request):
        calls.append(request)
        r = responses[min(len(calls), len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    return handler, calls


class TestGet:

    def test_retries_server_error_then_succeeds(self, mock_client):
        handler, calls = _counting([httpx.Response(503), httpx.Response(200, json={})])
        resp = get(mock_client(handler), "http://x/")
        assert resp.status_code == 200
        assert len(calls) == 2

    def test_returns_last_response_after_max_attempts(self, mock_client):
        handler, calls = _counting([httpx.Response(500)])
        resp = get(mock_client(handler), "http://x/")
        assert resp.status_code == 500
        assert len(calls) == MAX_ATTEMPTS

    def test_does_not_retry_404(self, mock_client):
        handler, calls = _counting([httpx.Response(404)])
        assert get(mock_client(handler), "http://x/").status_code == 404
        assert len(calls) == 1

    def test_honors_short_retry_after(self, mock_client, no_sleep):
        handler, _ = _counting([
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200),
        ])
        get(mock_client(handler), "http://x/")
        assert no_sleep == [7.0]

    def test_gives_up_on_huge_retry_after(self, mock_client):
        handler, calls = _counting([httpx.Response(429, headers={"Retry-After": "68400"})])
        assert get(mock_client(handler), "http://x/").status_code == 429
        assert len(calls) == 1

    def test_custom_retry_statuses(self, mock_client):
        handler, calls = _counting([httpx.Response(429)])
        get(mock_client(handler), "http://x/", retry_statuses=(500,))
        assert len(calls) == 1

    def test_network_error_retried_then_raised(self, mock_client):
        handler, calls = _counting([httpx.ConnectError("boom")])
        with pytest.raises(httpx.ConnectError):
            get(mock_client(handler), "http://x/")
        assert len(calls) == MAX_ATTEMPTS


class TestMakeClient:

    def test_email_in_user_agent(self):
        with make_client("me@example.org") as c:
            assert "mailto:me@example.org" in c.headers["User-Agent"]

    def test_user_agent_without_email(self):
        with make_client() as c:
            assert c.headers["User-Agent"].startswith("dataset-citation-finder/")
            assert "mailto" not in c.headers["User-Agent"]
