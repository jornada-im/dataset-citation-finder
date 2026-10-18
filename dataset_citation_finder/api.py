"""Public functions: find_citations, read_citations, dois_under_prefix."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import store
from .http_client import make_client
from .sources import SOURCES, BudgetExhausted, add_api_key, datacite_pages
from .utils import normalize_doi

log = logging.getLogger(__name__)

COLUMNS = ["cited_doi", "citing_doi", "sources", "source_count", "relation_types",
           "first_seen"]


def find_citations(
    dois: str | Iterable[str],
    *,
    db_path: str | Path | None = None,
    refresh_after: timedelta | None = None,
    sources: Iterable[str] = tuple(SOURCES),
    email: str | None = None,
    api_keys: dict[str, str] | None = None,
    progress: bool = True,
) -> pd.DataFrame:
    """Find papers that cite one or more dataset DOIs.

    Parameters
    ----------
    dois
        One DOI or a list of them, in any common form ("10.6073/...",
        "https://doi.org/10.6073/...", "doi:10.6073/..."). Invalid entries
        are skipped with a warning.
    db_path
        Optional SQLite file. Results are saved there as they arrive, and the
        returned table includes results from earlier runs. Use it for long
        lists, or lists you re-check periodically.
    refresh_after
        Requires `db_path`. Skip DOIs that a source answered within this
        window, e.g. `timedelta(days=30)`. Re-running with the same value
        also resumes an interrupted run.
    sources
        Which databases to query: any of "datacite_events", "openalex",
        "opencitations". Defaults to all three.
    email
        Your contact email, sent in the User-Agent as the APIs request.
    api_keys
        Optional keys, e.g. {"openalex": "...", "opencitations": "..."}.
        Missing keys are read from OPENALEX_API_KEY / OPENCITATIONS_API_KEY.
    progress
        Print progress lines to stderr.

    Returns
    -------
    DataFrame with one row per (cited_doi, citing_doi), restricted to the
    input DOIs, with columns:
        sources         which databases reported the citation, ";"-separated
        source_count    how many databases reported it
        relation_types  DataCite relation types, ";"-separated (may be empty)
        first_seen      when the citation was first found (UTC, ISO 8601)

    DOIs that failed (network or server errors) are listed per source in
    `df.attrs["failed_dois"]`. They aren't marked as queried, so the next run
    with the same `db_path` retries them.
    """
    dois = _clean_dois(dois)
    sources = list(sources)
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown}; choose from {list(SOURCES)}")
    if refresh_after is not None and db_path is None:
        raise ValueError("refresh_after needs a db_path to remember earlier runs")

    say = _printer(progress)
    if db_path is not None:
        store.open_store(db_path).close()  # create or check the file up front
    started = store.now()

    # Each source runs in its own thread, so a run takes as long as the
    # slowest source rather than the sum of all three. Within a source, DOIs
    # are still queried one at a time to respect that source's rate limit.
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        jobs = {
            name: pool.submit(
                _query_source, name, dois, db_path=db_path,
                refresh_after=refresh_after, email=email,
                api_key=(api_keys or {}).get(name) or _env_key(name),
                say=say, stop=stop,
            )
            for name in sources
        }
        try:
            for job in as_completed(jobs.values()):
                job.result()  # re-raise the first failure as soon as it happens
        except BaseException:  # including Ctrl-C
            stop.set()  # the other sources stop after their current DOI
            raise
        results = {name: job.result() for name, job in jobs.items()}

    failed = {name: errors for name, (_, errors) in results.items() if errors}
    if db_path is not None:
        conn = store.open_store(db_path)
        try:
            long = store.read_rows(conn)
        finally:
            conn.close()
        long = long[long["cited_doi"].isin(dois)]
    else:
        found = [row for rows, _ in results.values() for row in rows]
        long = pd.DataFrame(found, columns=["cited_doi", "citing_doi", "source",
                                            "relation_type"])
        long["first_seen"] = started

    result = collapse(long)
    result.attrs["failed_dois"] = failed
    if failed:
        say(f"{sum(map(len, failed.values()))} DOI lookups failed; see "
            "df.attrs['failed_dois']" + ("; re-run to retry them." if db_path else "."))
    return result


def _query_source(
    name: str,
    dois: list[str],
    *,
    db_path: str | Path | None,
    refresh_after: timedelta | None,
    email: str | None,
    api_key: str | None,
    say,
    stop: threading.Event,
) -> tuple[list[dict], list[str]]:
    """Query one source for each DOI in turn.

    With a store, each DOI's results are saved as soon as they arrive and
    the returned row list is empty. Returns (rows, failed DOIs).
    """
    conn = store.open_store(db_path) if db_path is not None else None
    found: list[dict] = []
    failed: list[str] = []
    try:
        todo = dois if conn is None else store.dois_to_query(conn, dois, name, refresh_after)
        if not todo:
            say(f"{name}: all {len(dois)} DOIs were checked recently; skipping.")
            return found, failed
        say(f"{name}: querying {len(todo)} DOIs...")
        with make_client(email) as client:
            if api_key:
                add_api_key(client, name, api_key)
            for i, doi in enumerate(todo, 1):
                if stop.is_set():
                    return found, failed
                try:
                    rows = SOURCES[name].fetch(client, doi)
                except BudgetExhausted as e:
                    say(f"{name}: {e}. Stopping {name}; re-run later to continue.")
                    failed.extend(todo[i - 1:])
                    break
                except Exception as e:
                    log.warning("%s: %s failed: %s", name, doi, e)
                    failed.append(doi)
                else:
                    if conn is not None:
                        store.save(conn, name, doi, rows)
                    else:
                        found.extend(rows)
                if i % 100 == 0:
                    say(f"{name}: {i}/{len(todo)}")
                time.sleep(SOURCES[name].delay)
        say(f"{name}: done ({len(todo) - len(failed)} answered, {len(failed)} failed).")
    finally:
        if conn is not None:
            conn.close()
    return found, failed


def read_citations(db_path: str | Path, *, raw: bool = False) -> pd.DataFrame:
    """Load everything saved in a store, without querying any database.

    Returns the same columns as `find_citations`. With `raw=True`, returns
    one row per (cited_doi, citing_doi, source, relation_type) instead, with
    `first_seen` and `last_seen` timestamps.
    """
    conn = store.open_store(db_path, create=False)
    try:
        rows = store.read_rows(conn)
    finally:
        conn.close()
    return rows if raw else collapse(rows)


def dois_under_prefix(prefix: str, *, email: str | None = None) -> list[str]:
    """List every DOI that DataCite has registered under `prefix` (e.g. "10.6073").

    Large prefixes take a while (EDI's 10.6073 is hundreds of requests), so
    save the result if you'll reuse it:

        Path("dois.txt").write_text("\\n".join(dois))
    """
    with make_client(email) as client:
        items = datacite_pages(client, "https://api.datacite.org/dois",
                               {"prefix": prefix, "fields[dois]": "doi"})
        return sorted({item["id"].lower() for item in items})


def collapse(long: pd.DataFrame) -> pd.DataFrame:
    """Merge per-source rows into one row per (cited_doi, citing_doi)."""
    if long.empty:
        return pd.DataFrame(columns=COLUMNS)

    def join(values) -> str:
        return ";".join(sorted({v for v in values if v}))

    out = long.groupby(["cited_doi", "citing_doi"], as_index=False).agg(
        sources=("source", join),
        source_count=("source", "nunique"),
        relation_types=("relation_type", join),
        first_seen=("first_seen", "min"),
    )
    return out[COLUMNS].reset_index(drop=True)


def _clean_dois(dois: str | Iterable[str]) -> list[str]:
    if isinstance(dois, str):
        dois = [dois]
    cleaned: dict[str, None] = {}  # a dict keeps input order while dropping repeats
    for raw in dois:
        doi = normalize_doi(raw)
        if doi is None:
            log.warning("skipping invalid DOI: %r", raw)
        else:
            cleaned[doi] = None
    if not cleaned:
        raise ValueError("no valid DOIs given")
    return list(cleaned)


def _env_key(source: str) -> str | None:
    env = SOURCES[source].api_key_env
    return os.environ.get(env) if env else None


def _printer(enabled: bool):
    def say(msg: str) -> None:
        if enabled:
            print(msg, file=sys.stderr, flush=True)
    return say
