"""OpenAlex adapter.

https://api.openalex.org/works?filter=cites:W<id>

OpenAlex indexes works by its own IDs (W-prefixed). To find citations of a
dataset DOI we resolve the DOI to an OpenAlex work, then page through works
that cite it.

Two efficiency measures keep request volume down on large prefix sweeps:

1. The resolve request asks for `select=id,cited_by_count` only (small payload).
2. If `cited_by_count == 0`, the citers-listing request is skipped entirely —
   most dataset DOIs have no OpenAlex citers, so this removes the (empty)
   listing request for the majority of inputs.

NOTE on resolution: we resolve via the per-DOI endpoint `/works/doi:{doi}`,
NOT the `filter=doi:…` batch filter. DataCite-sourced dataset works (e.g. EDI's
`10.6073/…`) exist in OpenAlex's entity store — reachable by direct DOI lookup —
but are missing from the searchable `filter=doi:` index, so a batch filter
silently drops them. The per-DOI endpoint resolves them reliably.

Not every dataset DOI is indexed in OpenAlex. An unindexed DOI (404) simply
yields nothing — a successful query with an empty result, not a failure.

Authentication: OpenAlex grants a substantially larger daily budget when an
API key is supplied via the `api_key` query parameter on every request. Sign
up at https://openalex.org/settings/api to get one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..http_client import RetryPolicy, request_with_retry
from ..schema import LongRow
from ..utils import normalize_doi
from .base import BudgetExhausted, SourceAdapter

log = logging.getLogger(__name__)

WORKS_URL = "https://api.openalex.org/works"

# Page size for the citers listing. OpenAlex's documented maximum has been 200;
# if a future change rejects it, lower to 100.
PER_PAGE = 200

RESOLVE_SELECT = "id,cited_by_count"
LISTING_SELECT = "id,doi,title,publication_year"

# For OpenAlex a 429 means the DAILY budget is exhausted (per-second throttling
# is 403); it will not clear within this run, so retrying it just burns backoff.
# Drop 429 from the retryable set so it surfaces immediately — we convert it to
# BudgetExhausted and stop the batch. 5xx stay retryable (transient).
_RETRY = RetryPolicy(retry_on_status=(500, 502, 503, 504))


def _raise_if_budget_exhausted(resp, doi: str) -> None:
    """Convert a daily-cap 429 into BudgetExhausted (stops the whole batch)."""
    if resp.status_code == 429:
        reset = resp.headers.get("X-RateLimit-Reset")
        log.warning(
            "openalex: daily request budget exhausted (HTTP 429) on %s; "
            "resets in ~%ss (midnight UTC). Stopping OpenAlex; re-run to resume.",
            doi, reset,
        )
        raise BudgetExhausted("openalex daily request budget exhausted")


class OpenAlexAdapter(SourceAdapter):
    source_id = "openalex"
    supports_prefix = False
    supports_batch = False
    # OpenAlex enforces a per-IP/per-key rate limit (~10 req/s) and a daily
    # request budget. Low concurrency + an inter-request delay keep us under
    # the per-second ceiling (the engine wraps each per-DOI call in the
    # semaphore); cited_by_count pruning (below) trims the daily total. An
    # api_key raises both ceilings but does not remove them, so the
    # conservative defaults stay either way.
    default_concurrency = 2
    inter_request_delay = 0.5  # seconds
    accepts_api_key = True

    def _auth_params(self) -> dict[str, str]:
        """Query params injected on every request when an API key is set."""
        return {"api_key": self._api_key} if self._api_key else {}

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        # Resolve DOI -> OpenAlex work (id + citation count only).
        resp = await request_with_retry(
            self._client, "GET", f"{WORKS_URL}/doi:{doi}",
            params={"select": RESOLVE_SELECT, **self._auth_params()},
            retry=_RETRY,
        )
        if resp.status_code == 404:
            return  # not in OpenAlex: empty result, not a failure
        _raise_if_budget_exhausted(resp, doi)
        if resp.status_code >= 400:
            log.warning("openalex: HTTP %d resolving %s", resp.status_code, doi)
            self._mark_failed(doi)
            return

        work = resp.json()
        work_id = work.get("id")  # e.g. "https://openalex.org/W123..."
        if not work_id:
            return
        # Prune: a work OpenAlex reports as uncited needs no listing request.
        if int(work.get("cited_by_count") or 0) == 0:
            return

        short_id = work_id.rsplit("/", 1)[-1]

        # Page through works that cite it.
        cursor: str | None = "*"
        while cursor:
            resp = await request_with_retry(
                self._client, "GET", WORKS_URL,
                params={
                    "filter": f"cites:{short_id}",
                    "select": LISTING_SELECT,
                    "per-page": PER_PAGE,
                    "cursor": cursor,
                    **self._auth_params(),
                },
                retry=_RETRY,
            )
            _raise_if_budget_exhausted(resp, doi)
            if resp.status_code >= 400:
                log.warning("openalex: HTTP %d listing citers of %s", resp.status_code, doi)
                self._mark_failed(doi)
                return
            body = resp.json()
            for item in body.get("results", []):
                citing_doi = normalize_doi(item.get("doi"))
                if not citing_doi:
                    continue  # work has no DOI; skip for dedup purposes
                try:
                    yield LongRow(
                        cited_doi=doi,
                        citing_doi=citing_doi,
                        source=self.source_id,
                        extra={
                            "openalex_id": item.get("id"),
                            "cited_openalex_id": work_id,
                            "title": item.get("title"),
                            "year": item.get("publication_year"),
                        },
                    )
                except ValueError:
                    continue

            cursor = (body.get("meta") or {}).get("next_cursor")
