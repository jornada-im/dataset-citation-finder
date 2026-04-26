"""OpenCitations Index v2 adapter.

https://opencitations.net/index/api/v2/citations/{doi}

Simple per-DOI REST API that returns a JSON list of citation objects.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..http_client import request_with_retry
from ..schema import LongRow
from .base import SourceAdapter

log = logging.getLogger(__name__)

BASE_URL = "https://opencitations.net/index/api/v2/citations"


class OpenCitationsAdapter(SourceAdapter):
    source_id = "opencitations"
    supports_prefix = False
    supports_batch = False
    # OpenCitations is a free service with low rate limits; keep concurrent
    # requests low and hold the slot briefly after each response to avoid
    # triggering 429s on large prefix sweeps.
    default_concurrency = 2
    inter_request_delay = 0.5  # seconds

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        url = f"{BASE_URL}/doi:{doi}"
        resp = await request_with_retry(self._client, "GET", url)
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            log.warning("opencitations: HTTP %d for %s", resp.status_code, doi)
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
