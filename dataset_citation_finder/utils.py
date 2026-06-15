"""Utility functions. The most important one is `normalize_doi`, which is
called by every adapter at its output boundary so dedup keys work regardless
of upstream formatting."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger(__name__)

_DOI_REGEX = re.compile(r"^10\.\d{4,9}/.+$")

_URL_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


def normalize_doi(raw: str | None) -> str | None:
    """Normalize a DOI string for use as a dedup key.

    Steps (in order): strip known URL/scheme prefixes, trim whitespace,
    URL-decode percent-encoded characters, lowercase, then validate against
    the DOI syntax pattern.

    Returns the normalized DOI, or None if the input is None, empty, or
    does not match DOI syntax. Malformed DOIs are logged at DEBUG.
    """
    if raw is None:
        return None

    s = raw.strip()
    if not s:
        return None

    # Strip URL/scheme prefixes (case-insensitive).
    lowered = s.lower()
    for prefix in _URL_PREFIXES:
        if lowered.startswith(prefix):
            s = s[len(prefix):]
            break

    s = unquote(s).strip().lower()

    if not _DOI_REGEX.match(s):
        log.debug("normalize_doi: rejected malformed DOI %r", raw)
        return None

    return s


def datacite_next_cursor(body: dict) -> str | None:
    """Extract the next-page cursor from a DataCite JSON:API response.

    DataCite (both /events and /dois) paginates via a `links.next` URL that
    embeds `page[cursor]=...`. Returns the cursor value, or None when there is
    no next page.
    """
    next_url = (body.get("links") or {}).get("next")
    if not next_url:
        return None
    cursors = parse_qs(urlparse(next_url).query).get("page[cursor]")
    return cursors[0] if cursors else None
