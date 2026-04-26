"""Tests for DataCiteEventsAdapter.

The DataCite Events API returns JSON:API-shaped responses. We mock paginated
results to exercise cursor handling, direction normalization, and relation
type filtering.
"""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.adapters.datacite_events import (
    DEFAULT_RELATION_TYPES,
    DataCiteEventsAdapter,
)


def _event(event_id, subj, obj, relation, source="crossref"):
    """Build a DataCite Events JSON:API-shaped event."""
    return {
        "id": event_id,
        "type": "events",
        "attributes": {
            "subj-id": subj,
            "obj-id": obj,
            "relation-type-id": relation,
            "source-id": source,
            "occurred-at": "2024-01-15T00:00:00Z",
        },
    }


def _response_body(events, next_url=None):
    return {"data": events, "links": {"next": next_url} if next_url else {}}


class TestDataCiteEventsAdapter:

    @pytest.mark.asyncio
    async def test_object_side_query_normalizes_direction(self, make_mock_client):
        """For `is-cited-by`: obj-id is the dataset, subj-id is the citer."""
        calls = []

        def handler(request):
            calls.append(dict(request.url.params))
            # First call: object-side with obj-id set
            if "obj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("evt-1",
                           subj="https://doi.org/10.1038/nature1",
                           obj="https://doi.org/10.6073/pasta/abc",
                           relation="is-cited-by"),
                ]))
            # Subject-side call returns empty
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].cited_doi == "10.6073/pasta/abc"
        assert rows[0].citing_doi == "10.1038/nature1"
        assert rows[0].relation_type == "is-cited-by"
        assert rows[0].source == "datacite_events"
        assert rows[0].extra["event_id"] == "evt-1"
        assert rows[0].extra["upstream_source"] == "crossref"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_references_relation_counted(self, make_mock_client):
        """CrossRef-sourced events use `references` — critical case."""
        def handler(request):
            if "obj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("evt-r",
                           subj="https://doi.org/10.1038/nature1",
                           obj="https://doi.org/10.6073/pasta/abc",
                           relation="references"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].relation_type == "references"
        assert rows[0].cited_doi == "10.6073/pasta/abc"
        assert rows[0].citing_doi == "10.1038/nature1"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_pagination_follows_cursor(self, make_mock_client):
        call_count = [0]

        def handler(request):
            # Only interested in object-side calls for this test.
            if "obj-id" not in request.url.params:
                return httpx.Response(200, json=_response_body([]))

            call_count[0] += 1
            cursor = request.url.params.get("page[cursor]")
            if cursor == "1":
                return httpx.Response(200, json=_response_body(
                    [_event("e1",
                            subj="https://doi.org/10.1038/a",
                            obj="https://doi.org/10.6073/pasta/abc",
                            relation="is-cited-by")],
                    next_url="https://api.datacite.org/events?page%5Bcursor%5D=2",
                ))
            if cursor == "2":
                return httpx.Response(200, json=_response_body(
                    [_event("e2",
                            subj="https://doi.org/10.1038/b",
                            obj="https://doi.org/10.6073/pasta/abc",
                            relation="is-cited-by")]
                ))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        citing = {r.citing_doi for r in rows}
        assert citing == {"10.1038/a", "10.1038/b"}
        # At least 2 object-side calls (cursor 1 + cursor 2).
        assert call_count[0] >= 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_filters_out_unwanted_relation_types(self, make_mock_client):
        """`is-version-of` must NOT count as a citation (structural relation)."""
        def handler(request):
            if "obj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("e1",
                           subj="https://doi.org/10.6073/pasta/v2",
                           obj="https://doi.org/10.6073/pasta/abc",
                           relation="is-version-of"),
                    _event("e2",
                           subj="https://doi.org/10.1038/citer",
                           obj="https://doi.org/10.6073/pasta/abc",
                           relation="is-cited-by"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].relation_type == "is-cited-by"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_prefix_query_uses_prefix_filter(self, make_mock_client):
        """Prefix mode should hit `obj-id.prefix=10.6073`."""
        seen_params = []

        def handler(request):
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=_response_body([
                _event("e1",
                       subj="https://doi.org/10.1038/citer",
                       obj="https://doi.org/10.6073/pasta/xyz",
                       relation="is-cited-by"),
            ]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_prefix("10.6073")]
        assert len(rows) == 1
        assert rows[0].cited_doi == "10.6073/pasta/xyz"
        # Verify the prefix filter was actually applied.
        assert any("obj-id.prefix" in p and p["obj-id.prefix"] == "10.6073" for p in seen_params)
        # And object-side relations were requested.
        assert any("is-cited-by" in p.get("relation-type-id", "") for p in seen_params)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_default_relation_types_includes_all_six(self):
        """Sanity check: per design §13, all six relation types are in the default."""
        assert set(DEFAULT_RELATION_TYPES) == {
            "is-cited-by", "is-referenced-by", "is-supplement-to",
            "cites", "references", "is-supplemented-by",
        }

    @pytest.mark.asyncio
    async def test_bare_doi_in_event(self, make_mock_client):
        """Event IDs may be bare DOIs without https://doi.org/ prefix."""
        def handler(request):
            if "obj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("e1",
                           subj="10.1038/a",   # no URL prefix
                           obj="10.6073/pasta/abc",
                           relation="is-cited-by"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].citing_doi == "10.1038/a"
        await client.aclose()
