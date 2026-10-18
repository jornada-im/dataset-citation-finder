"""DOI normalization. Every source runs its DOIs through `normalize_doi` so
results from different databases can be matched on the same key."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

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
