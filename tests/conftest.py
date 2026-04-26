"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture
def make_mock_client():
    """Factory returning an httpx.AsyncClient wired to a user-supplied handler.

    Usage in a test:

        def handler(request):
            return httpx.Response(200, json={...})

        client = make_mock_client(handler)
    """
    clients = []

    def _factory(handler):
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="http://mocked")
        clients.append(client)
        return client

    yield _factory

    # Teardown: close any clients that weren't closed in the test.
    import asyncio

    async def _close_all():
        for c in clients:
            if not c.is_closed:
                await c.aclose()

    try:
        asyncio.get_event_loop().run_until_complete(_close_all())
    except RuntimeError:
        # No running loop / event loop closed — fine.
        pass
