"""Tests for the per-source fetch functions, with HTTP mocked."""

from __future__ import annotations

import httpx
import pytest

from dataset_citation_finder.sources import (
    BudgetExhausted,
    add_api_key,
    fetch_datacite_events,
    fetch_openalex,
    fetch_opencitations,
)

DATASET = "10.6073/pasta/abc"


def _citers(rows):
    return {r["citing_doi"] for r in rows}


# ---- DataCite Events ---------------------------------------------------------

def _event(subj, obj, relation):
    return {"id": "evt", "attributes": {
        "subj-id": subj, "obj-id": obj, "relation-type-id": relation,
    }}


def _page(events, next_url=None):
    return {"data": events, "links": {"next": next_url} if next_url else {}}


class TestDataCiteEvents:

    def test_filters_use_full_doi_url(self, mock_client):
        """A bare DOI in obj-id/subj-id silently matches nothing upstream."""
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=_page([]))

        assert fetch_datacite_events(mock_client(handler), DATASET) == []
        assert {p.get("obj-id") for p in seen} >= {f"https://doi.org/{DATASET}"}
        assert {p.get("subj-id") for p in seen} >= {f"https://doi.org/{DATASET}"}

    def test_both_directions_normalized(self, mock_client):
        def handler(request):
            if "obj-id" in request.url.params:   # paper references dataset
                return httpx.Response(200, json=_page([_event(
                    "https://doi.org/10.1038/a", f"https://doi.org/{DATASET}", "references")]))
            return httpx.Response(200, json=_page([_event(  # dataset is-cited-by paper
                f"https://doi.org/{DATASET}", "https://doi.org/10.1038/B", "is-cited-by")]))

        rows = fetch_datacite_events(mock_client(handler), DATASET)
        assert sorted((r["citing_doi"], r["relation_type"]) for r in rows) == [
            ("10.1038/a", "references"), ("10.1038/b", "is-cited-by")]
        assert all(r["cited_doi"] == DATASET and r["source"] == "datacite_events"
                   for r in rows)

    def test_pagination_keeps_filters(self, mock_client):
        def handler(request):
            p = request.url.params
            if "subj-id" not in p:
                return httpx.Response(200, json=_page([]))
            subj = f"https://doi.org/{DATASET}"
            if p["page[cursor]"] == "1":
                return httpx.Response(200, json=_page(
                    [_event(subj, "10.1038/a", "is-cited-by")],
                    next_url="https://api.datacite.org/events?page%5Bcursor%5D=xyz"))
            assert p["page[cursor]"] == "xyz"
            return httpx.Response(200, json=_page([_event(subj, "10.1038/b", "is-cited-by")]))

        rows = fetch_datacite_events(mock_client(handler), DATASET)
        assert _citers(rows) == {"10.1038/a", "10.1038/b"}

    def test_skips_other_relations_and_non_doi_citers(self, mock_client):
        def handler(request):
            if "obj-id" not in request.url.params:
                return httpx.Response(200, json=_page([]))
            return httpx.Response(200, json=_page([
                _event("10.1038/a", DATASET, "is-version-of"),
                _event("https://example.org/page", DATASET, "references"),
                _event("10.1038/ok", DATASET, "cites"),
            ]))

        assert _citers(fetch_datacite_events(mock_client(handler), DATASET)) == {"10.1038/ok"}

    def test_server_error_raises(self, mock_client):
        handler = lambda request: httpx.Response(500)  # noqa: E731
        with pytest.raises(httpx.HTTPStatusError):
            fetch_datacite_events(mock_client(handler), DATASET)


# ---- OpenAlex ----------------------------------------------------------------

def _openalex(resolve, pages=()):
    """Handler: `resolve` answers /works/doi:..., `pages` answer the listing in order."""
    calls = {"resolve": 0, "list": []}

    def handler(request):
        if request.url.path.startswith("/works/doi:"):
            calls["resolve"] += 1
            return resolve
        calls["list"].append(dict(request.url.params))
        return pages[len(calls["list"]) - 1]
    return handler, calls


def _work(count=2):
    return httpx.Response(200, json={"id": "https://openalex.org/W9", "cited_by_count": count})


def _results(dois, next_cursor=None):
    return httpx.Response(200, json={"results": [{"doi": d} for d in dois],
                                     "meta": {"next_cursor": next_cursor}})


class TestOpenAlex:

    def test_resolves_then_lists_citers(self, mock_client):
        handler, calls = _openalex(_work(), [
            _results(["https://doi.org/10.1038/a", None], next_cursor="c2"),
            _results(["https://doi.org/10.1038/b"]),
        ])
        rows = fetch_openalex(mock_client(handler), DATASET)
        assert _citers(rows) == {"10.1038/a", "10.1038/b"}
        assert calls["list"][0]["filter"] == "cites:W9"
        assert calls["list"][1]["cursor"] == "c2"

    def test_not_indexed_is_empty_not_error(self, mock_client):
        handler, _ = _openalex(httpx.Response(404))
        assert fetch_openalex(mock_client(handler), DATASET) == []

    def test_uncited_work_skips_listing(self, mock_client):
        handler, calls = _openalex(_work(count=0))
        assert fetch_openalex(mock_client(handler), DATASET) == []
        assert calls["list"] == []

    def test_429_is_budget_exhausted_without_retry(self, mock_client):
        handler, calls = _openalex(httpx.Response(429))
        with pytest.raises(BudgetExhausted):
            fetch_openalex(mock_client(handler), DATASET)
        assert calls["resolve"] == 1

    def test_429_while_listing_is_budget_exhausted(self, mock_client):
        handler, _ = _openalex(_work(), [httpx.Response(429)])
        with pytest.raises(BudgetExhausted):
            fetch_openalex(mock_client(handler), DATASET)

    def test_403_fails_without_retry(self, mock_client):
        handler, calls = _openalex(httpx.Response(403))
        with pytest.raises(httpx.HTTPStatusError):
            fetch_openalex(mock_client(handler), DATASET)
        assert calls["resolve"] == 1

    def test_api_key_sent_as_query_param(self, mock_client):
        seen = []

        def handler(request):
            seen.append(request.url.params.get("api_key"))
            return httpx.Response(404)

        client = mock_client(handler)
        add_api_key(client, "openalex", "secret")
        fetch_openalex(client, DATASET)
        assert seen == ["secret"]


# ---- OpenCitations -----------------------------------------------------------

class TestOpenCitations:

    def test_parses_citing_dois(self, mock_client):
        def handler(request):
            assert request.url.host == "api.opencitations.net"
            return httpx.Response(200, json=[
                {"citing": "omid:br/1 doi:10.1038/A pmid:123"},
                {"citing": "omid:br/2 pmid:456"},   # no DOI: skipped
            ])

        rows = fetch_opencitations(mock_client(handler), DATASET)
        assert rows == [{"cited_doi": DATASET, "citing_doi": "10.1038/a",
                         "source": "opencitations", "relation_type": ""}]

    def test_404_is_empty(self, mock_client):
        handler = lambda request: httpx.Response(404)  # noqa: E731
        assert fetch_opencitations(mock_client(handler), DATASET) == []

    def test_server_error_raises(self, mock_client):
        handler = lambda request: httpx.Response(503)  # noqa: E731
        with pytest.raises(httpx.HTTPStatusError):
            fetch_opencitations(mock_client(handler), DATASET)

    def test_api_key_sent_as_bare_authorization_header(self, mock_client):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        client = mock_client(handler)
        add_api_key(client, "opencitations", "tok")
        fetch_opencitations(client, DATASET)
        assert seen == ["tok"]
