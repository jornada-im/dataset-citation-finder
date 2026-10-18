"""Check that DOIs are registered, before spending time finding citations.

Separate from `find_citations`, which never calls it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
import pandas as pd

from .http_client import get, make_client
from .utils import normalize_doi

log = logging.getLogger(__name__)


def validate_dois(
    dois: str | Iterable[str],
    *,
    check_registrar: bool = True,
    email: str | None = None,
) -> pd.DataFrame:
    """Check whether each DOI resolves at doi.org.

    Returns one row per input with columns:
        doi        the input as given
        status     "registered" (doi.org redirects), "unresolved" (404),
                   "invalid" (not DOI syntax) or "unknown" (anything else)
        registrar  "datacite", "crossref" or None. Only filled in for
                   registered DOIs when `check_registrar` is True. A
                   "crossref" DOI in a dataset list is usually a paper DOI
                   pasted in by mistake.
        notes      details for "unknown" results
    """
    if isinstance(dois, str):
        dois = [dois]
    results = []
    with make_client(email) as client:
        for raw in dois:
            doi = normalize_doi(raw)
            status, notes, registrar = "invalid", None, None
            if doi is not None:
                status, notes = _check_handle(client, doi)
                if status == "registered" and check_registrar:
                    registrar = _registrar(client, doi)
            results.append({"doi": raw, "status": status,
                            "registrar": registrar, "notes": notes})
    return pd.DataFrame(results, columns=["doi", "status", "registrar", "notes"])


def _check_handle(client: httpx.Client, doi: str) -> tuple[str, str | None]:
    try:
        # A redirect *is* the success signal, so don't follow it.
        resp = client.head(f"https://doi.org/{doi}", follow_redirects=False)
    except httpx.HTTPError as e:
        return "unknown", f"network error: {e!r}"
    if resp.is_redirect:
        return "registered", None
    if resp.status_code == 404:
        return "unresolved", None
    return "unknown", f"doi.org returned HTTP {resp.status_code}"


def _registrar(client: httpx.Client, doi: str) -> str | None:
    for name, url in (("datacite", f"https://api.datacite.org/dois/{doi}"),
                      ("crossref", f"https://api.crossref.org/works/{doi}")):
        try:
            if get(client, url, max_attempts=2).is_success:
                return name
        except httpx.HTTPError as e:
            log.debug("%s lookup for %s failed: %s", name, doi, e)
    return None
