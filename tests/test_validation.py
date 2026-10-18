"""Tests for validate_dois, with HTTP mocked."""

from __future__ import annotations

import httpx

from dataset_citation_finder import validation
from dataset_citation_finder.validation import validate_dois


def handler(request):
    url = str(request.url)
    if request.url.host == "doi.org":
        if "missing" in url:
            return httpx.Response(404)
        if "flaky" in url:
            return httpx.Response(500)
        return httpx.Response(302, headers={"Location": "https://example.org/"})
    if request.url.host == "api.datacite.org":
        return httpx.Response(200 if "6073" in url else 404, json={})
    if request.url.host == "api.crossref.org":
        return httpx.Response(200, json={})
    return httpx.Response(500)


def test_classifies_each_doi(mock_client, monkeypatch):
    monkeypatch.setattr(validation, "make_client", lambda email=None: mock_client(handler))
    df = validate_dois(["10.6073/pasta/ok", "10.1038/paper", "10.6073/missing",
                        "10.6073/flaky", "not a doi"])
    assert df.fillna("").to_dict("records") == [
        {"doi": "10.6073/pasta/ok", "status": "registered", "registrar": "datacite",
         "notes": ""},
        {"doi": "10.1038/paper", "status": "registered", "registrar": "crossref",
         "notes": ""},
        {"doi": "10.6073/missing", "status": "unresolved", "registrar": "",
         "notes": ""},
        {"doi": "10.6073/flaky", "status": "unknown", "registrar": "",
         "notes": "doi.org returned HTTP 500"},
        {"doi": "not a doi", "status": "invalid", "registrar": "", "notes": ""},
    ]


def test_skip_registrar_check(mock_client, monkeypatch):
    monkeypatch.setattr(validation, "make_client", lambda email=None: mock_client(handler))
    df = validate_dois("10.6073/pasta/ok", check_registrar=False)
    assert df.loc[0, "status"] == "registered"
    assert df.loc[0, "registrar"] is None
