"""Tests for find_citations / read_citations / dois_under_prefix.

The real sources are swapped for fakes, so no HTTP happens.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from datetime import timedelta

import httpx
import pytest

from dataset_citation_finder import (
    StoreError,
    dois_under_prefix,
    find_citations,
    read_citations,
)
from dataset_citation_finder import api
from dataset_citation_finder.sources import SOURCES, BudgetExhausted


class FakeSource:
    """A fetch function that returns canned citers per DOI and logs calls.

    `citers` maps dataset DOI -> list of citing DOIs. A value that is an
    exception instance is raised instead.
    """

    def __init__(self, name, citers, relation=""):
        self.name, self.citers, self.relation = name, citers, relation
        self.calls: list[str] = []
        self.clients: list[httpx.Client] = []

    def __call__(self, client, doi):
        self.calls.append(doi)
        self.clients.append(client)
        result = self.citers.get(doi, [])
        if isinstance(result, BaseException):
            raise result
        return [{"cited_doi": doi, "citing_doi": c, "source": self.name,
                 "relation_type": self.relation} for c in result]


@pytest.fixture
def fakes(monkeypatch):
    dc = FakeSource("datacite_events", {"10.1000/a": ["10.9999/p1", "10.9999/p2"]}, "references")
    oa = FakeSource("openalex", {"10.1000/a": ["10.9999/p1"], "10.1000/b": ["10.9999/p3"]})
    oc = FakeSource("opencitations", {})
    for f in (dc, oa, oc):
        # Keep each source's real settings (delay, API-key env var); swap the fetch.
        monkeypatch.setitem(SOURCES, f.name, dataclasses.replace(SOURCES[f.name], fetch=f))
    return dc, oa, oc


class TestInMemory:

    def test_merges_sources_per_citation(self, fakes):
        df = find_citations(["10.1000/a", "https://doi.org/10.1000/B"], progress=False)
        assert list(df.columns) == api.COLUMNS
        by_pair = {(r.cited_doi, r.citing_doi): r for r in df.itertuples()}
        assert set(by_pair) == {("10.1000/a", "10.9999/p1"), ("10.1000/a", "10.9999/p2"),
                                ("10.1000/b", "10.9999/p3")}
        shared = by_pair[("10.1000/a", "10.9999/p1")]
        assert shared.sources == "datacite_events;openalex"
        assert shared.source_count == 2
        assert shared.relation_types == "references"
        assert df.attrs["failed_dois"] == {}

    def test_single_doi_string(self, fakes):
        assert len(find_citations("doi:10.1000/A", progress=False)) == 2

    def test_no_citations_gives_empty_frame_with_columns(self, fakes):
        df = find_citations("10.1000/none", progress=False)
        assert df.empty and list(df.columns) == api.COLUMNS

    def test_invalid_and_duplicate_inputs(self, fakes):
        dc, _, _ = fakes
        find_citations(["10.1000/a", "junk", "10.1000/A"], sources=["datacite_events"],
                       progress=False)
        assert dc.calls == ["10.1000/a"]
        with pytest.raises(ValueError, match="no valid DOIs"):
            find_citations(["junk"], progress=False)

    def test_source_subset_and_unknown_source(self, fakes):
        df = find_citations("10.1000/a", sources=["openalex"], progress=False)
        assert set(df["sources"]) == {"openalex"}
        with pytest.raises(ValueError, match="unknown source"):
            find_citations("10.1000/a", sources=["google_scholar"], progress=False)

    def test_refresh_after_requires_db(self, fakes):
        with pytest.raises(ValueError, match="db_path"):
            find_citations("10.1000/a", refresh_after=timedelta(days=1), progress=False)

    def test_failed_doi_reported_others_kept(self, fakes):
        _, oa, _ = fakes
        oa.citers["10.1000/a"] = httpx.ConnectError("down")
        df = find_citations(["10.1000/a", "10.1000/b"], progress=False)
        assert df.attrs["failed_dois"] == {"openalex": ["10.1000/a"]}
        assert ("10.1000/b", "10.9999/p3") in set(zip(df.cited_doi, df.citing_doi))

    def test_budget_exhausted_stops_only_that_source(self, fakes):
        dc, oa, _ = fakes
        oa.citers["10.1000/a"] = BudgetExhausted("daily cap")
        df = find_citations(["10.1000/a", "10.1000/b", "10.1000/c"], progress=False)
        assert oa.calls == ["10.1000/a"]
        assert df.attrs["failed_dois"] == {"openalex": ["10.1000/a", "10.1000/b", "10.1000/c"]}
        assert dc.calls == ["10.1000/a", "10.1000/b", "10.1000/c"]

    def test_api_key_from_env_or_argument(self, fakes, monkeypatch):
        _, oa, oc = fakes
        monkeypatch.setenv("OPENALEX_API_KEY", "from-env")
        monkeypatch.setenv("OPENCITATIONS_API_KEY", "from-env")
        find_citations("10.1000/a", api_keys={"opencitations": "explicit"}, progress=False)
        assert oa.clients[0].params["api_key"] == "from-env"
        assert oc.clients[0].headers["authorization"] == "explicit"

    def test_progress_goes_to_stderr(self, fakes, capsys):
        find_citations("10.1000/a", sources=["openalex"])
        err = capsys.readouterr().err
        assert "openalex: querying 1 DOIs" in err
        assert "1 answered, 0 failed" in err


class TestWithStore:

    def test_saves_and_returns_same_shape(self, fakes, tmp_path):
        db = tmp_path / "c.sqlite"
        df = find_citations(["10.1000/a", "10.1000/b"], db_path=db, progress=False)
        assert len(df) == 3
        assert read_citations(db).equals(df)
        raw = read_citations(db, raw=True)
        assert len(raw) == 4  # p1 is stored once per source
        assert {"first_seen", "last_seen"} <= set(raw.columns)

    def test_refresh_after_skips_recent_but_returns_stored(self, fakes, tmp_path):
        dc, _, _ = fakes
        db = tmp_path / "c.sqlite"
        find_citations(["10.1000/a"], db_path=db, progress=False)
        dc.calls.clear()
        df = find_citations(["10.1000/a", "10.1000/b"], db_path=db,
                            refresh_after=timedelta(days=30), progress=False)
        assert dc.calls == ["10.1000/b"]  # 10.1000/a was answered moments ago
        assert len(df) == 3            # ...but its citations are still returned

    def test_no_refresh_after_requeries_everything(self, fakes, tmp_path):
        dc, _, _ = fakes
        db = tmp_path / "c.sqlite"
        find_citations("10.1000/a", db_path=db, progress=False)
        find_citations("10.1000/a", db_path=db, progress=False)
        assert dc.calls == ["10.1000/a", "10.1000/a"]

    def test_failed_doi_retried_within_refresh_window(self, fakes, tmp_path):
        _, oa, _ = fakes
        db = tmp_path / "c.sqlite"
        oa.citers["10.1000/b"] = httpx.ConnectError("down")
        find_citations(["10.1000/a", "10.1000/b"], db_path=db, progress=False)
        oa.calls.clear()
        oa.citers["10.1000/b"] = ["10.9999/p3"]
        df = find_citations(["10.1000/a", "10.1000/b"], db_path=db,
                            refresh_after=timedelta(days=30), progress=False)
        assert oa.calls == ["10.1000/b"]
        assert ("10.1000/b", "10.9999/p3") in set(zip(df.cited_doi, df.citing_doi))

    def test_result_limited_to_requested_dois(self, fakes, tmp_path):
        db = tmp_path / "c.sqlite"
        find_citations(["10.1000/a", "10.1000/b"], db_path=db, progress=False)
        df = find_citations("10.1000/b", db_path=db, refresh_after=timedelta(days=1),
                            progress=False)
        assert set(df["cited_doi"]) == {"10.1000/b"}

    def test_first_seen_kept_last_seen_advances(self, fakes, tmp_path, monkeypatch):
        db = tmp_path / "c.sqlite"
        monkeypatch.setattr(api.store, "now", lambda: "2026-01-01T00:00:00+00:00")
        find_citations("10.1000/b", db_path=db, sources=["openalex"], progress=False)
        monkeypatch.setattr(api.store, "now", lambda: "2026-02-01T00:00:00+00:00")
        df = find_citations("10.1000/b", db_path=db, sources=["openalex"], progress=False)
        raw = read_citations(db, raw=True)
        assert raw.loc[0, "first_seen"] == "2026-01-01T00:00:00+00:00"
        assert raw.loc[0, "last_seen"] == "2026-02-01T00:00:00+00:00"
        assert df.loc[0, "first_seen"] == "2026-01-01T00:00:00+00:00"

    def test_interrupted_run_resumes(self, fakes, tmp_path, monkeypatch):
        """Ctrl-C (here: raised inside openalex) stops every source after its
        current DOI; re-running with refresh_after queries only the rest."""
        dc, oa, _ = fakes
        dois = [f"10.1000/d{i}" for i in range(500)]
        dc_fetch, oa_fetch = SOURCES["datacite_events"].fetch, SOURCES["openalex"].fetch
        dc_busy = threading.Event()

        def dc_slowly(client, doi):
            if len(dc.calls) == 10:
                dc_busy.set()
            dc_busy.wait(0.002)  # a real pause, so the interrupt lands mid-run
            return dc_fetch(client, doi)

        def oa_interrupted(client, doi):
            dc_busy.wait(5)  # let datacite get some DOIs done first
            raise KeyboardInterrupt

        for name, fetch in (("datacite_events", dc_slowly), ("openalex", oa_interrupted)):
            monkeypatch.setitem(SOURCES, name, dataclasses.replace(SOURCES[name], fetch=fetch))
        db = tmp_path / "c.sqlite"
        with pytest.raises(KeyboardInterrupt):
            find_citations(dois, db_path=db, sources=["datacite_events", "openalex"],
                           progress=False)
        done = len(dc.calls)
        assert 10 <= done < len(dois)

        dc.calls.clear()
        monkeypatch.setitem(SOURCES, "openalex",
                            dataclasses.replace(SOURCES["openalex"], fetch=oa_fetch))
        find_citations(dois, db_path=db, sources=["datacite_events", "openalex"],
                       refresh_after=timedelta(days=1), progress=False)
        assert len(dc.calls) == len(dois) - done
        assert len(oa.calls) == len(dois)

    def test_rejects_foreign_sqlite_file(self, fakes, tmp_path):
        db = tmp_path / "old.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE source_openalex (x)")
        with pytest.raises(StoreError, match="not created by this version"):
            find_citations("10.1000/a", db_path=db, progress=False)

    def test_read_citations_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_citations(tmp_path / "nope.sqlite")


class TestDoisUnderPrefix:

    def test_pages_through_datacite(self, mock_client, monkeypatch):
        def handler(request):
            assert request.url.params["prefix"] == "10.6073"
            if request.url.params["page[cursor]"] == "1":
                return httpx.Response(200, json={
                    "data": [{"id": "10.6073/B"}],
                    "links": {"next": "https://api.datacite.org/dois?page%5Bcursor%5D=2"},
                })
            return httpx.Response(200, json={"data": [{"id": "10.6073/a"}], "links": {}})

        monkeypatch.setattr(api, "make_client", lambda email=None: mock_client(handler))
        assert dois_under_prefix("10.6073") == ["10.6073/a", "10.6073/b"]
