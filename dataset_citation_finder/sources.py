"""The citation databases we query, one function per source.

Every fetch function has the same shape:

    fetch(client, doi) -> list of {"cited_doi", "citing_doi", "source", "relation_type"}

`doi` is an already-normalized dataset DOI and `cited_doi` is always that
DOI. A fetch function returns an empty list when the source has no citations
for the DOI (including "DOI not found"), and raises on any other failure so
the caller can retry that DOI later.

To add a source: write a fetch function and add it to `SOURCES`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from .http_client import get
from .utils import normalize_doi

log = logging.getLogger(__name__)


class BudgetExhausted(Exception):
    """A source's daily request quota is used up; stop querying it this run."""


def _row(cited_doi: str, citing: str | None, source: str,
         relation_type: str | None = None) -> dict | None:
    """Build a result row, or None if the citing identifier isn't a DOI."""
    citing_doi = normalize_doi(citing)
    if citing_doi is None:
        return None
    return {
        "cited_doi": cited_doi,
        "citing_doi": citing_doi,
        "source": source,
        "relation_type": relation_type or "",
    }


# ---- DataCite Event Data -------------------------------------------------
#
# DataCite events read "subj <relation> obj". Depending on the relation, the
# dataset is either the object ("paper cites dataset") or the subject
# ("dataset is-cited-by paper"). All six relation types count as citations:
# CrossRef-derived events arrive as `references`, so keeping only
# `is-cited-by` would miss most real citations.

DATACITE_EVENTS_URL = "https://api.datacite.org/events"
DATASET_IS_OBJECT = ("cites", "is-supplemented-by", "references")
DATASET_IS_SUBJECT = ("is-cited-by", "is-referenced-by", "is-supplement-to")


def fetch_datacite_events(client: httpx.Client, doi: str) -> list[dict]:
    rows = []
    # The obj-id / subj-id filters only match the full https://doi.org/ form;
    # a bare DOI silently matches nothing.
    doi_url = f"https://doi.org/{doi}"
    for dataset_side, citer_side, relations in (
        ("obj-id", "subj-id", DATASET_IS_OBJECT),
        ("subj-id", "obj-id", DATASET_IS_SUBJECT),
    ):
        params = {dataset_side: doi_url, "relation-type-id": ",".join(relations)}
        for event in datacite_pages(client, DATACITE_EVENTS_URL, params):
            attrs = event.get("attributes") or {}
            relation = attrs.get("relation-type-id")
            if relation not in relations:
                continue
            row = _row(doi, attrs.get(citer_side), "datacite_events", relation)
            if row:
                rows.append(row)
    return rows


def datacite_pages(client: httpx.Client, url: str, params: dict) -> Iterator[dict]:
    """Yield every item from a cursor-paginated DataCite API listing."""
    params = {**params, "page[size]": 1000, "page[cursor]": "1"}
    while True:
        resp = get(client, url, params=params)
        resp.raise_for_status()
        body = resp.json()
        yield from body.get("data", [])
        # The `next` link embeds the cursor for the following page.
        next_url = (body.get("links") or {}).get("next")
        cursor = parse_qs(urlparse(next_url).query).get("page[cursor]") if next_url else None
        if not cursor:
            return
        params["page[cursor]"] = cursor[0]


# ---- OpenAlex ----------------------------------------------------------------
#
# OpenAlex identifies works by its own IDs, so we first resolve the DOI to a
# work, then page through the works that cite it. Resolve with the per-DOI
# endpoint (/works/doi:...), never the `filter=doi:` search: the search index
# misses DataCite-registered datasets (e.g. EDI's 10.6073) that resolve fine
# by direct lookup.
#
# Rate limits: an HTTP 429 means the *daily* budget is spent and won't reset
# during this run, so it is not retried; we raise BudgetExhausted and stop
# querying OpenAlex. A 403 (per-second throttle, or a bad key) is treated as
# an ordinary per-DOI failure — retrying it would add ~15s of backoff to every
# remaining DOI if the cause is persistent.

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_RETRY_STATUSES = (500, 502, 503, 504)


def fetch_openalex(client: httpx.Client, doi: str) -> list[dict]:
    resp = get(client, f"{OPENALEX_WORKS_URL}/doi:{doi}",
               params={"select": "id,cited_by_count"},
               retry_statuses=OPENALEX_RETRY_STATUSES)
    if resp.status_code == 404:
        return []  # dataset isn't indexed in OpenAlex
    _check_openalex(resp)
    work = resp.json()
    # Most datasets have no OpenAlex citers; skip the listing request for them.
    if not work.get("id") or not work.get("cited_by_count"):
        return []

    work_id = work["id"].rsplit("/", 1)[-1]  # "https://openalex.org/W123" -> "W123"
    rows = []
    cursor = "*"
    while cursor:
        resp = get(client, OPENALEX_WORKS_URL, params={
            "filter": f"cites:{work_id}",
            "select": "doi",
            "per-page": 200,
            "cursor": cursor,
        }, retry_statuses=OPENALEX_RETRY_STATUSES)
        _check_openalex(resp)
        body = resp.json()
        results = body.get("results", [])
        for item in results:
            row = _row(doi, item.get("doi"), "openalex")
            if row:
                rows.append(row)
        cursor = (body.get("meta") or {}).get("next_cursor") if results else None
    return rows


def _check_openalex(resp: httpx.Response) -> None:
    if resp.status_code == 429:
        raise BudgetExhausted(
            "OpenAlex daily request budget is used up (resets at midnight UTC)"
        )
    resp.raise_for_status()


# ---- OpenCitations -----------------------------------------------------------
#
# Call api.opencitations.net directly: the legacy opencitations.net path
# redirects there, and httpx drops the Authorization header on cross-host
# redirects, which would silently send keyed requests as anonymous.

OPENCITATIONS_URL = "https://api.opencitations.net/index/v2/citations"


def fetch_opencitations(client: httpx.Client, doi: str) -> list[dict]:
    resp = get(client, f"{OPENCITATIONS_URL}/doi:{doi}")
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    records = resp.json()
    if not isinstance(records, list):
        raise ValueError(f"unexpected OpenCitations response for {doi}")

    rows = []
    for record in records:
        # "citing" is a space-separated list of ids, e.g. "omid:br/06 doi:10.1/x".
        citing_ids = (record.get("citing") or "").split()
        citing_doi = next((i[4:] for i in citing_ids if i.startswith("doi:")), None)
        row = _row(doi, citing_doi, "opencitations")
        if row:
            rows.append(row)
    return rows


# ---- registry ----------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    fetch: Callable[[httpx.Client, str], list[dict]]
    # Seconds to pause after each DOI, to stay under the source's rate limit.
    delay: float = 0.0
    # Environment variable holding this source's API key, if it takes one.
    api_key_env: str | None = None


SOURCES: dict[str, Source] = {
    "datacite_events": Source(fetch_datacite_events),
    # ~10 requests/second per IP or key; a DOI costs 1-2+ requests.
    "openalex": Source(fetch_openalex, delay=0.1, api_key_env="OPENALEX_API_KEY"),
    # A free service with low anonymous limits.
    "opencitations": Source(fetch_opencitations, delay=0.2,
                            api_key_env="OPENCITATIONS_API_KEY"),
}


def add_api_key(client: httpx.Client, source: str, api_key: str) -> None:
    """Attach an API key to `client` the way `source` expects it."""
    if source == "openalex":
        client.params = client.params.set("api_key", api_key)
    elif source == "opencitations":
        # The bare token, with no "Bearer " prefix.
        client.headers["authorization"] = api_key
