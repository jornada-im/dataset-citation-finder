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
    async def test_subject_side_query_normalizes_direction(self, make_mock_client):
        """For `is-cited-by`: subj-id is the dataset, obj-id is the citer."""
        def handler(request):
            # Subject-side query: filter by subj-id.
            if "subj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("evt-1",
                           subj="https://doi.org/10.6073/pasta/abc",
                           obj="https://doi.org/10.1038/nature1",
                           relation="is-cited-by"),
                ]))
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
        """CrossRef-sourced events use `references` (object-side: dataset on obj)."""
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
            # Only interested in subject-side calls (is-cited-by) for this test.
            if "subj-id" not in request.url.params:
                return httpx.Response(200, json=_response_body([]))

            call_count[0] += 1
            cursor = request.url.params.get("page[cursor]")
            if cursor == "1":
                return httpx.Response(200, json=_response_body(
                    [_event("e1",
                            subj="https://doi.org/10.6073/pasta/abc",
                            obj="https://doi.org/10.1038/a",
                            relation="is-cited-by")],
                    next_url="https://api.datacite.org/events?page%5Bcursor%5D=2",
                ))
            if cursor == "2":
                return httpx.Response(200, json=_response_body(
                    [_event("e2",
                            subj="https://doi.org/10.6073/pasta/abc",
                            obj="https://doi.org/10.1038/b",
                            relation="is-cited-by")]
                ))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 2
        citing = {r.citing_doi for r in rows}
        assert citing == {"10.1038/a", "10.1038/b"}
        # At least 2 subject-side calls (cursor 1 + cursor 2).
        assert call_count[0] >= 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_filters_out_unwanted_relation_types(self, make_mock_client):
        """`is-version-of` must NOT count as a citation (structural relation)."""
        def handler(request):
            if "subj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("e1",
                           subj="https://doi.org/10.6073/pasta/abc",
                           obj="https://doi.org/10.6073/pasta/v2",
                           relation="is-version-of"),
                    _event("e2",
                           subj="https://doi.org/10.6073/pasta/abc",
                           obj="https://doi.org/10.1038/citer",
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
        """Prefix mode should hit `prefix=10.6073` (NOT `obj-id.prefix`)
        and split the query by relation-type side."""
        seen_params = []

        def handler(request):
            params = dict(request.url.params)
            seen_params.append(params)
            relations = params.get("relation-type-id", "")
            # Subject-side query (is-cited-by): dataset on subj-id.
            if "is-cited-by" in relations:
                return httpx.Response(200, json=_response_body([
                    _event("e1",
                           subj="https://doi.org/10.6073/pasta/xyz",
                           obj="https://doi.org/10.1038/citer",
                           relation="is-cited-by"),
                ]))
            # Object-side query (cites/references): dataset on obj-id.
            if "references" in relations or "cites" in relations:
                return httpx.Response(200, json=_response_body([
                    _event("e2",
                           subj="https://doi.org/10.1038/other-citer",
                           obj="https://doi.org/10.6073/pasta/qrs",
                           relation="references"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_prefix("10.6073")]
        cited = {r.cited_doi for r in rows}
        assert cited == {"10.6073/pasta/xyz", "10.6073/pasta/qrs"}
        # The correct prefix filter was used (NOT obj-id.prefix).
        assert all("obj-id.prefix" not in p for p in seen_params)
        assert any(p.get("prefix") == "10.6073" for p in seen_params)
        # Each cited_doi starts with the requested prefix (regression guard).
        for r in rows:
            assert r.cited_doi.startswith("10.6073")

    @pytest.mark.asyncio
    async def test_prefix_query_drops_events_outside_prefix(self, make_mock_client):
        """When `prefix=` matches a non-dataset participant rather than the
        dataset itself, the resulting row's cited_doi won't start with the
        prefix and must be dropped."""
        def handler(request):
            params = dict(request.url.params)
            relations = params.get("relation-type-id", "")
            # Subject-side: subj-id is supposed to be the dataset, but here
            # we return an event where subj-id has the wrong prefix (the
            # `prefix=` filter happened to match obj-id instead).
            if "is-cited-by" in relations:
                return httpx.Response(200, json=_response_body([
                    _event("e-bad",
                           subj="https://doi.org/10.1038/not-edi",
                           obj="https://doi.org/10.6073/pasta/something",
                           relation="is-cited-by"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_prefix("10.6073")]
        assert rows == []
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
            if "subj-id" in request.url.params:
                return httpx.Response(200, json=_response_body([
                    _event("e1",
                           subj="10.6073/pasta/abc",   # no URL prefix
                           obj="10.1038/a",
                           relation="is-cited-by"),
                ]))
            return httpx.Response(200, json=_response_body([]))

        client = make_mock_client(handler)
        adapter = DataCiteEventsAdapter(client)
        rows = [r async for r in adapter.query_single("10.6073/pasta/abc")]
        assert len(rows) == 1
        assert rows[0].citing_doi == "10.1038/a"
        await client.aclose()
