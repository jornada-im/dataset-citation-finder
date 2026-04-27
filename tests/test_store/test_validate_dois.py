"""Tests for validate_dois.

Uses httpx.MockTransport to stub the doi.org handle resolver and the
DataCite/CrossRef metadata endpoints.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from dataset_citation_finder import (
    RunSummary,
    open_store,
    read_citations,
    validate_dois,
)
from dataset_citation_finder import api as api_module


def _make_handler(registry: dict[str, int]):
    """Return an httpx handler that classifies routes by URL prefix.

    `registry` maps path-fragments → status code we should return for the
    handle-resolver call. DataCite/CrossRef probes always return 200 for the
    fragments listed.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "doi.org" in url:
            for fragment, code in registry.items():
                if fragment in url:
                    if 300 <= code < 400:
                        return httpx.Response(code, headers={"Location": "https://example.org"})
                    return httpx.Response(code)
            return httpx.Response(404)
        if "api.datacite.org/dois/" in url:
            for fragment in registry:
                if fragment in url and "datacite-yes" in fragment:
                    return httpx.Response(200, json={"data": {"id": fragment}})
            return httpx.Response(404)
        if "api.crossref.org/works/" in url:
            for fragment in registry:
                if fragment in url and "crossref-yes" in fragment:
                    return httpx.Response(200, json={"message": {"DOI": fragment}})
            return httpx.Response(404)
        return httpx.Response(500)
    return handler


@pytest.fixture
def patch_make_client(monkeypatch):
    """Replace api.make_client with a mock-transport variant."""
    captured = {}

    def install(handler):
        def fake_make_client(*, email=None, user_agent=None):
            transport = httpx.MockTransport(handler)
            return httpx.AsyncClient(transport=transport, base_url="http://mocked")
        monkeypatch.setattr(api_module, "make_client", fake_make_client)
        # Also patch the validation module's import path for request_with_retry-
        # wrapped calls (DataCite/CrossRef probes).
        captured["installed"] = True

    return install


class TestValidateDoisInMemory:

    def test_returns_dataframe_when_no_db(self, patch_make_client):
        patch_make_client(_make_handler({
            "10.1234/registered-datacite-yes": 302,
            "10.1234/missing": 404,
        }))
        df = validate_dois(["10.1234/registered-datacite-yes", "10.1234/missing"])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        statuses = dict(zip(df["cited_doi"], df["validation_status"]))
        assert statuses["10.1234/registered-datacite-yes"] == "registered"
        assert statuses["10.1234/missing"] == "unresolved"

    def test_invalid_dois_raise_when_all_invalid(self, patch_make_client):
        with pytest.raises(ValueError, match="no valid DOIs"):
            validate_dois(["not-a-doi"])


class TestValidateDoisToStore:

    def test_persists_to_cited_dois_and_returns_run_summary(
        self, patch_make_client, tmp_path
    ):
        patch_make_client(_make_handler({
            "10.1234/registered-datacite-yes": 302,
            "10.1234/missing": 404,
        }))
        db = tmp_path / "store.sqlite"
        summary = validate_dois(
            ["10.1234/registered-datacite-yes", "10.1234/missing"],
            db_path=db, check_registrar=False,
        )
        assert isinstance(summary, RunSummary)
        assert summary.inputs_total == 2
        assert summary.rows_per_source == {"doi.org": 2}

        df = read_citations(db, view="cited_dois")
        statuses = dict(zip(df["cited_doi"], df["validation_status"]))
        assert statuses["10.1234/registered-datacite-yes"] == "registered"
        assert statuses["10.1234/missing"] == "unresolved"
        # Origin should be 'validation' for DOIs added via this path.
        origins = dict(zip(df["cited_doi"], df["origin"]))
        assert origins["10.1234/missing"] == "validation"

        # doi_validations has one audit row per DOI.
        audit = read_citations(db, view="doi_validations")
        assert len(audit) == 2
        assert set(audit["method"]) == {"doi.org"}

    def test_run_row_recorded_with_validate_kind(
        self, patch_make_client, tmp_path
    ):
        patch_make_client(_make_handler({"10.1234/x": 302}))
        db = tmp_path / "store.sqlite"
        validate_dois(["10.1234/x"], db_path=db, check_registrar=False)

        conn = open_store(db, create=False)
        try:
            row = conn.execute(
                "SELECT input_kind, input_count FROM runs"
            ).fetchone()
        finally:
            conn.close()
        assert row["input_kind"] == "validate"
        assert row["input_count"] == 1


class TestRefreshAfter:

    def test_refresh_after_skips_recently_validated(
        self, patch_make_client, tmp_path
    ):
        from datetime import timedelta

        patch_make_client(_make_handler({"10.1234/x": 302}))
        db = tmp_path / "store.sqlite"

        first = validate_dois(["10.1234/x"], db_path=db, check_registrar=False)
        assert first.rows_per_source["doi.org"] == 1

        # Re-run with a 30-day window; should skip.
        second = validate_dois(
            ["10.1234/x"], db_path=db,
            refresh_after=timedelta(days=30),
            check_registrar=False,
        )
        assert second.inputs_skipped == 1
        assert second.rows_per_source["doi.org"] == 0

    def test_force_overrides_refresh_after(
        self, patch_make_client, tmp_path
    ):
        from datetime import timedelta

        patch_make_client(_make_handler({"10.1234/x": 302}))
        db = tmp_path / "store.sqlite"
        validate_dois(["10.1234/x"], db_path=db, check_registrar=False)
        second = validate_dois(
            ["10.1234/x"], db_path=db,
            refresh_after=timedelta(days=30), force=True,
            check_registrar=False,
        )
        assert second.inputs_skipped == 0
        assert second.rows_per_source["doi.org"] == 1
