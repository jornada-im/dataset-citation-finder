"""Tests for api_keys.resolve_api_keys + adapter wiring."""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder import resolve_api_keys
from dataset_citation_finder.adapters.opencitations import OpenCitationsAdapter
from dataset_citation_finder.adapters.openalex import OpenAlexAdapter


class TestResolveApiKeys:

    def test_explicit_dict_passthrough(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        out = resolve_api_keys({"openalex": "explicit-key"})
        assert out == {"openalex": "explicit-key"}

    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "from-env")
        out = resolve_api_keys()
        assert out["openalex"] == "from-env"

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "from-env")
        out = resolve_api_keys({"openalex": "explicit"})
        assert out["openalex"] == "explicit"

    def test_missing_keys_omitted(self, monkeypatch):
        for env in ("OPENALEX_API_KEY", "OPENCITATIONS_API_KEY",
                    "SEMANTIC_SCHOLAR_API_KEY", "DIMENSIONS_API_KEY"):
            monkeypatch.delenv(env, raising=False)
        out = resolve_api_keys()
        assert out == {}

    def test_opencitations_env_fallback(self, monkeypatch):
        monkeypatch.setenv("OPENCITATIONS_API_KEY", "oc-from-env")
        out = resolve_api_keys()
        assert out["opencitations"] == "oc-from-env"

    def test_blank_values_dropped(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        out = resolve_api_keys({"openalex": ""})
        assert "openalex" not in out

    def test_unknown_source_passes_through(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        # Forward-compatible: keys for sources we don't yet enumerate are kept.
        out = resolve_api_keys({"future_source": "k"})
        assert out == {"future_source": "k"}


class TestOpenAlexAdapterAuth:

    @pytest.mark.asyncio
    async def test_api_key_added_to_query_params(self, make_mock_client):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            # Resolve-DOI step.
            if "/works/doi:" in str(request.url):
                return httpx.Response(200, json={"id": "https://openalex.org/W123"})
            # Listing step — return empty so we exit immediately.
            return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client, api_key="my-secret-key")
        # Drain the generator.
        rows = [r async for r in adapter.query_single("10.1234/abc")]
        assert rows == []
        # Both the resolve-DOI request and the citers request must include the key.
        assert all(p.get("api_key") == "my-secret-key" for p in seen_params), seen_params
        assert len(seen_params) >= 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_api_key_means_no_auth_param(self, make_mock_client):
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            if "/works/doi:" in str(request.url):
                return httpx.Response(200, json={"id": "https://openalex.org/W123"})
            return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

        client = make_mock_client(handler)
        adapter = OpenAlexAdapter(client)  # no api_key
        _ = [r async for r in adapter.query_single("10.1234/abc")]
        assert all("api_key" not in p for p in seen_params), seen_params
        await client.aclose()


class TestOpenCitationsAdapterAuth:

    @pytest.mark.asyncio
    async def test_api_key_added_to_authorization_header(self, make_mock_client):
        seen_headers = []

        def handler(request):
            seen_headers.append(dict(request.headers))
            return httpx.Response(200, json=[])

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client, api_key="oc-secret")
        _ = [r async for r in adapter.query_single("10.1234/abc")]
        # OpenCitations expects the bare token in the `authorization` header.
        assert seen_headers[0].get("authorization") == "oc-secret"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_api_key_means_no_auth_header(self, make_mock_client):
        seen_headers = []

        def handler(request):
            seen_headers.append(dict(request.headers))
            return httpx.Response(200, json=[])

        client = make_mock_client(handler)
        adapter = OpenCitationsAdapter(client)  # no api_key
        _ = [r async for r in adapter.query_single("10.1234/abc")]
        assert "authorization" not in seen_headers[0]
        await client.aclose()


class TestApiBuildAdapters:
    """_build_adapters should only pass api_key to adapters that accept it."""

    def test_keys_routed_to_supporting_adapters_only(self, monkeypatch):
        from dataset_citation_finder import api as api_module

        # Stand-in client (we never make real requests in this test).
        class _DummyClient: pass
        client = _DummyClient()

        adapters = api_module._build_adapters(
            ["openalex", "opencitations", "datacite_events"],
            client,
            concurrency=4,
            adapter_opts={},
            api_keys={
                "openalex": "oa-key",
                "opencitations": "oc-key",
                "datacite_events": "ignored",
            },
        )
        oa = next(a for a in adapters if a.source_id == "openalex")
        oc = next(a for a in adapters if a.source_id == "opencitations")
        dce = next(a for a in adapters if a.source_id == "datacite_events")
        assert oa._api_key == "oa-key"
        assert oc._api_key == "oc-key"
        # DataCite doesn't authenticate: the key in the dict is ignored and
        # never reaches the adapter (every adapter has `_api_key`, None here).
        assert dce.accepts_api_key is False
        assert dce._api_key is None
