"""ShoulderEnumerator: fetch all DOIs under a given prefix from DataCite.

Used by per-DOI adapters during a shoulder sweep to obtain the working DOI
list. Results are cached on disk (JSON) keyed on the prefix.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .http_client import request_with_retry

log = logging.getLogger(__name__)

DOIS_URL = "https://api.datacite.org/dois"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 h


class ShoulderEnumerator:
    """Enumerate DOIs under a DataCite shoulder prefix, with disk caching."""

    def __init__(
        self,
        client,
        *,
        cache_dir: str | Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        page_size: int = 1000,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._page_size = page_size
        self._cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def enumerate(self, prefix: str) -> list[str]:
        """Return the full list of DOIs under `prefix`."""
        cached = self._read_cache(prefix)
        if cached is not None:
            log.debug("shoulder enumerator: cache hit for %s (%d DOIs)", prefix, len(cached))
            return cached

        dois: list[str] = []
        cursor: str | None = "1"
        while cursor is not None:
            params = {
                "query": f"prefix:{prefix}",
                "page[size]": self._page_size,
                "page[cursor]": cursor,
            }
            resp = await request_with_retry(self._client, "GET", DOIS_URL, params=params)
            if resp.status_code >= 400:
                log.warning("shoulder enumerator: HTTP %d for prefix=%s", resp.status_code, prefix)
                break
            body = resp.json()
            for item in body.get("data", []):
                doi = item.get("id") or (item.get("attributes") or {}).get("doi")
                if doi:
                    dois.append(doi.lower())
            cursor = self._next_cursor(body)

        self._write_cache(prefix, dois)
        return dois

    # ---- caching --------------------------------------------------------

    def _cache_path(self, prefix: str) -> Path | None:
        if self._cache_dir is None:
            return None
        safe = prefix.replace("/", "_")
        return self._cache_dir / f"shoulder_{safe}.json"

    def _read_cache(self, prefix: str) -> list[str] | None:
        path = self._cache_path(prefix)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("shoulder cache: failed to read %s: %s", path, e)
            return None
        if time.time() - payload.get("ts", 0) > self._ttl:
            return None
        return payload.get("dois")

    def _write_cache(self, prefix: str, dois: list[str]) -> None:
        path = self._cache_path(prefix)
        if path is None:
            return
        try:
            path.write_text(json.dumps({"ts": time.time(), "dois": dois}))
        except OSError as e:
            log.warning("shoulder cache: failed to write %s: %s", path, e)

    @staticmethod
    def _next_cursor(body: dict) -> str | None:
        from urllib.parse import parse_qs, urlparse
        links = body.get("links") or {}
        next_url = links.get("next")
        if not next_url:
            return None
        qs = parse_qs(urlparse(next_url).query)
        cursors = qs.get("page[cursor]")
        return cursors[0] if cursors else None
