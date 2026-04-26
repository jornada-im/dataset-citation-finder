"""Public API: sync entry points.

These are the only functions users should need to import:
    - find_citations(doi, ...)
    - find_citations_batch(dois, ...)
    - find_citations_by_prefix(prefix, ...)
    - collapse_sources(df)  # re-exported from collapse module
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import pandas as pd

from .adapters import DEFAULT_SOURCES, REGISTRY
from .adapters.base import SourceAdapter
from .collapse import collapse_sources
from .engine import collect_rows, rows_to_dataframe, rows_to_grouped
from .enumeration import PrefixEnumerator
from .http_client import make_client
from .schema import GroupedRecord
from .utils import normalize_doi

log = logging.getLogger(__name__)

__all__ = [
    "find_citations",
    "find_citations_batch",
    "find_citations_by_prefix",
    "collapse_sources",
]


def find_citations(
    doi: str,
    *,
    sources: Iterable[str] | str = "all",
    output: str = "long",
    email: str | None = None,
    concurrency: int = 8,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord]:
    """Find citations of a single dataset DOI.

    Parameters
    ----------
    doi : str
        The dataset DOI.
    sources : Iterable[str] | str
        Adapter IDs to query, or 'all' for the default set.
    output : str
        'long' (default) → pandas DataFrame in long format.
        'grouped' → list of GroupedRecord (one per unique citing work).
    email : str, optional
        Contact email for polite-pool headers.
    concurrency : int
        Per-adapter worker count.
    **adapter_opts : Any
        Reserved for per-adapter options (e.g. relation_types for DataCite).

    Returns
    -------
    pd.DataFrame | list[GroupedRecord]
    """
    norm = normalize_doi(doi)
    if norm is None:
        raise ValueError(f"invalid DOI: {doi!r}")
    return _run(dois=[norm], prefix=None, sources=sources, output=output,
                email=email, concurrency=concurrency, **adapter_opts)


def find_citations_batch(
    dois: Iterable[str],
    *,
    sources: Iterable[str] | str = "all",
    output: str = "long",
    email: str | None = None,
    concurrency: int = 8,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord]:
    """Find citations for an arbitrary list of dataset DOIs."""
    normed = []
    for d in dois:
        n = normalize_doi(d)
        if n is None:
            log.warning("skipping invalid DOI: %r", d)
            continue
        normed.append(n)
    if not normed:
        raise ValueError("no valid DOIs in input")
    return _run(dois=normed, prefix=None, sources=sources, output=output,
                email=email, concurrency=concurrency, **adapter_opts)


def find_citations_by_prefix(
    prefix: str,
    *,
    sources: Iterable[str] | str = "all",
    output: str = "long",
    email: str | None = None,
    concurrency: int = 8,
    cache_dir: str | None = None,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord]:
    """Find citations for every DOI under a DOI prefix.

    Prefix-capable adapters (DataCite Events) query their native prefix
    endpoint. Per-DOI adapters receive an enumerated DOI list from
    PrefixEnumerator (cached on disk).
    """
    prefix = prefix.lower().rstrip("/")
    return _run(dois=None, prefix=prefix, sources=sources, output=output,
                email=email, concurrency=concurrency, cache_dir=cache_dir,
                **adapter_opts)


# ---- internal -----------------------------------------------------------

def _resolve_sources(sources: Iterable[str] | str) -> list[str]:
    if sources == "all":
        return list(DEFAULT_SOURCES)
    return list(sources)


def _build_adapters(
    source_ids: list[str],
    client,
    concurrency: int,
    adapter_opts: dict[str, Any],
) -> list[SourceAdapter]:
    adapters: list[SourceAdapter] = []
    for sid in source_ids:
        cls = REGISTRY.get(sid)
        if cls is None:
            log.warning("unknown source %r; skipping", sid)
            continue
        effective_concurrency = min(concurrency, cls.default_concurrency)
        kwargs: dict[str, Any] = {"concurrency": effective_concurrency}
        # Forward adapter-specific options.
        if sid == "datacite_events" and "relation_types" in adapter_opts:
            kwargs["relation_types"] = adapter_opts["relation_types"]
        adapters.append(cls(client, **kwargs))
    return adapters


def _run(
    *,
    dois: list[str] | None,
    prefix: str | None,
    sources: Iterable[str] | str,
    output: str,
    email: str | None,
    concurrency: int,
    cache_dir: str | None = None,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord]:
    source_ids = _resolve_sources(sources)

    async def _pipeline() -> pd.DataFrame | list[GroupedRecord]:
        async with make_client(email=email) as client:
            adapters = _build_adapters(source_ids, client, concurrency, adapter_opts)
            if not adapters:
                raise ValueError("no adapters resolved from requested sources")

            # For prefix queries: enumerate DOIs for the per-DOI adapters.
            effective_dois = dois
            if prefix is not None:
                non_prefix_adapters = [a for a in adapters if not a.supports_prefix]
                prefix_adapters = [a for a in adapters if a.supports_prefix]

                rows_prefix = await collect_rows(prefix_adapters, prefix=prefix) if prefix_adapters else []

                if non_prefix_adapters:
                    enumerator = PrefixEnumerator(client, cache_dir=cache_dir)
                    enumerated = await enumerator.enumerate(prefix)
                    log.info("prefix %s: %d DOIs enumerated", prefix, len(enumerated))
                    rows_per_doi = await collect_rows(non_prefix_adapters, dois=enumerated) if enumerated else []
                else:
                    rows_per_doi = []

                rows = rows_prefix + rows_per_doi
            else:
                rows = await collect_rows(adapters, dois=effective_dois)

        return _shape_output(rows, output)

    return asyncio.run(_pipeline())


def _shape_output(rows, output: str):
    if output == "long":
        return rows_to_dataframe(rows)
    if output == "grouped":
        return rows_to_grouped(rows)
    raise ValueError(f"unknown output shape: {output!r}")
