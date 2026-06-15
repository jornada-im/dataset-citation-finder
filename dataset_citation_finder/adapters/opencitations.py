"""OpenCitations Index v2 adapter.

https://api.opencitations.net/index/v2/citations/{doi}

Simple per-DOI REST API that returns a JSON list of citation objects.

Note: the older `opencitations.net/index/api/v2/...` path 301-redirects to
the canonical `api.opencitations.net/index/v2/...`. httpx strips the
`Authorization` header across hosts by default, so calling the legacy URL
silently drops the API token. We always call the canonical host directly.

Authentication: OpenCitations honors an opaque API token passed via the
`authorization` request header (the bare token, no "Bearer" prefix). See
https://api.opencitations.net/index/v2#description for details. A token
raises rate limits substantially over anonymous access.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..http_client import request_with_retry
from ..schema import LongRow
from .base import SourceAdapter

log = logging.getLogger(__name__)

BASE_URL = "https://api.opencitations.net/index/v2/citations"


class OpenCitationsAdapter(SourceAdapter):
    source_id = "opencitations"
    supports_prefix = False
    supports_batch = False
    # OpenCitations is a free service with low rate limits; keep concurrent
    # requests low and hold the slot briefly after each response to avoid
    # triggering 429s on large prefix sweeps. Setting an api_key relaxes the
    # limits but the conservative defaults stay safe either way.
    default_concurrency = 2
    inter_request_delay = 0.5  # seconds
    accepts_api_key = True

    def _auth_headers(self) -> dict[str, str]:
        """Headers injected on every request when an API key is set.

        OpenCitations expects the bare token (no `Bearer ` prefix) in the
        `authorization` header — see the API docs.
        """
        return {"authorization": self._api_key} if self._api_key else {}

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        url = f"{BASE_URL}/doi:{doi}"
        resp = await request_with_retry(
            self._client, "GET", url,
            headers=self._auth_headers() or None,
        )
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            log.warning("opencitations: HTTP %d for %s", resp.status_code, doi)
            self._mark_failed(doi)
            return

        records = resp.json()
        if not isinstance(records, list):
            log.warning("opencitations: unexpected payload shape for %s", doi)
            return

        for rec in records:
            citing = rec.get("citing")
            if not citing:
                continue
            # OpenCitations returns prefixed identifiers like "doi:10.1234/..."
            citing_doi = _strip_doi_prefix(citing)
            if not citing_doi:
                continue
            try:
                yield LongRow(
                    cited_doi=doi,
                    citing_doi=citing_doi,
                    source=self.source_id,
                    extra={
                        "creation": rec.get("creation"),
                        "journal_sc": rec.get("journal_sc"),
                        "author_sc": rec.get("author_sc"),
                        "oci": rec.get("oci"),
                    },
                )
            except ValueError:
                continue


def _strip_doi_prefix(value: str) -> str | None:
    """OpenCitations returns 'doi:10.xxx/yyy' or sometimes 'pmid:...' etc.

    Only DOI-prefixed identifiers are useful to us.
    """
    if not isinstance(value, str):
        return None
    # Handle space-separated multi-id strings; pick the first DOI.
    for token in value.split():
        if token.startswith("doi:"):
            return token[4:]
    return None
