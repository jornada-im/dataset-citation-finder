"""OpenAlex adapter.

https://api.openalex.org/works?filter=cites:W<id>

OpenAlex indexes works by its own IDs (W-prefixed). To find citations of a
dataset DOI we first resolve the DOI to an OpenAlex work, then page through
works that cite it.

Not every dataset DOI will resolve in OpenAlex — many datasets aren't
indexed as works at all. In that case we yield nothing (not an error).

Authentication: OpenAlex offers a free "premium" tier with substantially
higher rate limits when an API key is supplied via the `api_key` query
parameter on every request. Sign up at https://openalex.org/ to get one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from ..http_client import request_with_retry
from ..schema import LongRow
from ..utils import normalize_doi
from .base import SourceAdapter

log = logging.getLogger(__name__)

WORKS_URL = "https://api.openalex.org/works"


class OpenAlexAdapter(SourceAdapter):
    source_id = "openalex"
    supports_prefix = False
    supports_batch = False

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        concurrency: int = 8,
        api_key: str | None = None,
    ) -> None:
        super().__init__(client, concurrency=concurrency)
        self._api_key = api_key
        if api_key:
            log.info("openalex: api_key attached (len=%d, ...%s)",
                     len(api_key), api_key[-4:])
        else:
            log.info("openalex: no api_key (polite pool only)")

    def _auth_params(self) -> dict[str, str]:
        """Query params injected on every request when an API key is set."""
        return {"api_key": self._api_key} if self._api_key else {}

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        # Resolve DOI -> OpenAlex work ID.
        resp = await request_with_retry(
            self._client, "GET", f"{WORKS_URL}/doi:{doi}",
            params=self._auth_params() or None,
        )
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            log.warning("openalex: HTTP %d resolving %s", resp.status_code, doi)
            self._mark_failed(doi)
            return

        work = resp.json()
        work_id = work.get("id")  # e.g. "https://openalex.org/W123..."
        if not work_id:
            return
        # Strip URL prefix to get the bare ID.
        short_id = work_id.rsplit("/", 1)[-1]

        # Page through works that cite it.
        cursor = "*"
        params: dict[str, str | int] = {
            "filter": f"cites:{short_id}",
            "per-page": 200,
            "select": "id,doi,title,publication_year,authorships,primary_location",
            **self._auth_params(),
        }
        while cursor:
            params["cursor"] = cursor
            resp = await request_with_retry(self._client, "GET", WORKS_URL, params=params)
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
                            "title": item.get("title"),
                            "year": item.get("publication_year"),
                        },
                    )
                except ValueError:
                    continue

            cursor = (body.get("meta") or {}).get("next_cursor")
