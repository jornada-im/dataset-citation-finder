"""DOI registration / resolvability validation.

Decoupled from the citation-fetch pipeline (see design §5.3, §6.5). Hits the
DOI handle resolver to check whether each DOI is registered, and optionally
probes DataCite and CrossRef metadata APIs to identify the registrar and
capture minimal metadata.

Reuses the existing `httpx` client; no new dependency.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import httpx

from .http_client import RetryPolicy, request_with_retry
from .schema import LongRow  # for type proximity only
from .store.upsert import (
    append_doi_validation,
    fetch_validation_freshness,
    set_validation_status,
)

log = logging.getLogger(__name__)

DOI_HANDLE_URL = "https://doi.org/{}"
DATACITE_DOI_URL = "https://api.datacite.org/dois/{}"
CROSSREF_WORK_URL = "https://api.crossref.org/works/{}"


# Status constants. See design §6.5.
STATUS_REGISTERED = "registered"
STATUS_UNRESOLVED = "unresolved"
STATUS_UNKNOWN = "unknown"


async def _check_handle(client: httpx.AsyncClient, doi: str) -> tuple[str, int | None, str]:
    """Issue HEAD https://doi.org/<doi>; classify the response.

    Returns (status, http_status, notes).
    """
    url = DOI_HANDLE_URL.format(doi)
    try:
        # No follow_redirects; a redirect *is* the success signal.
        resp = await client.head(url, follow_redirects=False)
    except httpx.HTTPError as e:
        return STATUS_UNKNOWN, None, f"transport error: {e!r}"

    code = resp.status_code
    if 300 <= code < 400:
        return STATUS_REGISTERED, code, ""
    if code == 404:
        return STATUS_UNRESOLVED, code, ""
    return STATUS_UNKNOWN, code, f"unexpected handle response {code}"


async def _probe_registrar(
    client: httpx.AsyncClient, doi: str
) -> tuple[str | None, str | None]:
    """Try DataCite then CrossRef metadata endpoints.

    Returns (registrar, notes). Registrar is 'datacite', 'crossref', or None.
    """
    try:
        resp = await request_with_retry(
            client, "GET", DATACITE_DOI_URL.format(doi),
            retry=RetryPolicy(max_attempts=2),
        )
        if 200 <= resp.status_code < 300:
            return "datacite", None
    except Exception as e:
        log.debug("datacite probe for %s failed: %s", doi, e)

    try:
        resp = await request_with_retry(
            client, "GET", CROSSREF_WORK_URL.format(doi),
            retry=RetryPolicy(max_attempts=2),
        )
        if 200 <= resp.status_code < 300:
            return "crossref", None
    except Exception as e:
        log.debug("crossref probe for %s failed: %s", doi, e)

    return None, None


async def _validate_one(
    client: httpx.AsyncClient,
    doi: str,
    *,
    check_registrar: bool,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Validate a single DOI. Returns a result dict suitable for both the
    in-memory DataFrame and the persisted rows."""
    async with semaphore:
        status, http_status, notes = await _check_handle(client, doi)
        registrar: str | None = None
        if status == STATUS_REGISTERED and check_registrar:
            registrar, _ = await _probe_registrar(client, doi)
        return {
            "cited_doi": doi,
            "validation_status": status,
            "http_status": http_status,
            "validated_registrar": registrar,
            "notes": notes or None,
            "validated_at": datetime.now(tz=timezone.utc).isoformat(),
        }


async def validate_dois_async(
    dois: Iterable[str],
    *,
    client: httpx.AsyncClient,
    check_registrar: bool = True,
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    """Async core; returns a list of result dicts, one per input DOI.

    Public sync entry point is `dataset_citation_finder.validate_dois`.
    """
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _validate_one(client, d, check_registrar=check_registrar, semaphore=semaphore)
        for d in dois
    ]
    return await asyncio.gather(*tasks)


def persist_validation_results(
    conn: sqlite3.Connection,
    results: list[dict[str, Any]],
    *,
    run_id: int,
) -> None:
    """Write each result into cited_dois (denormalized) and doi_validations."""
    for r in results:
        set_validation_status(
            conn,
            cited_doi=r["cited_doi"],
            status=r["validation_status"],
            validated_at=r["validated_at"],
            registrar=r["validated_registrar"],
        )
        append_doi_validation(
            conn,
            cited_doi=r["cited_doi"],
            validated_at=r["validated_at"],
            method="doi.org",
            status=r["validation_status"],
            http_status=r["http_status"],
            registrar=r["validated_registrar"],
            notes=r["notes"],
            run_id=run_id,
        )


def filter_by_validation_freshness(
    conn: sqlite3.Connection,
    *,
    dois: list[str],
    refresh_after,
    force: bool,
) -> tuple[list[str], int]:
    """Skip DOIs whose `validated_at` is newer than now-refresh_after."""
    if force or refresh_after is None or not dois:
        return dois, 0
    cutoff = (datetime.now(tz=timezone.utc) - refresh_after).isoformat()
    last = fetch_validation_freshness(conn, cited_dois=dois)
    kept = [d for d in dois if last.get(d, "") < cutoff]
    return kept, len(dois) - len(kept)
