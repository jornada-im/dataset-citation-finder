"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make retry backoff and per-source delays instant. Returns the list of
    requested sleep durations so tests can assert on them."""
    calls: list[float] = []
    monkeypatch.setattr("time.sleep", calls.append)
    return calls


@pytest.fixture
def mock_client():
    """Factory: `mock_client(handler)` returns an httpx.Client whose requests
    go to `handler(request) -> httpx.Response` instead of the network."""
    clients = []

    def _factory(handler, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    yield _factory
    for c in clients:
        c.close()
