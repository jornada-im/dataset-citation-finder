"""Public API: sync entry points.

Single-DOI lookups (`find_citations`) are in-memory by default and may
optionally write through to a SQLite store. Batch and prefix runs require
a `db_path` and persist to that store, returning a `RunSummary` describing
what was written.

Helpers:
    - coalesce_citations(db_path) — rebuild the canonical `citations` table
    - export_citations(db_path, output_path, ...) — derive CSV/Parquet/etc.
    - read_citations(db_path, view=...) — load a table or view as DataFrame
    - open_store / describe_store — store-layer access
    - collapse_citations_df — in-memory long DataFrame → deduplicated DataFrame
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .adapters import DEFAULT_SOURCES, REGISTRY
from .adapters.base import SourceAdapter
from .api_keys import resolve_api_keys
from .collapse import collapse_citations_df
from .engine import (
    StoreCollectResult,
    collect_rows,
    collect_to_store,
    rows_to_dataframe,
    rows_to_grouped,
)
from .enumeration import PrefixEnumerator
from .http_client import make_client
from .schema import GroupedRecord
from .store import describe_store, open_store, StoreInfo
from .store.coalesce import coalesce_citations as _coalesce_in_store
from .store.schema import SOURCE_TABLES
from .store.upsert import (
    fetch_freshness,
    finish_run,
    insert_run,
    upsert_cited_dois,
)
from .validation import (
    filter_by_validation_freshness,
    persist_validation_results,
    validate_dois_async,
)
from .utils import normalize_doi, utcnow

log = logging.getLogger(__name__)

__all__ = [
    "find_citations",
    "find_citations_batch",
    "find_citations_by_prefix",
    "validate_dois",
    "coalesce_citations",
    "export_citations",
    "read_citations",
    "open_store",
    "describe_store",
    "StoreInfo",
    "RunSummary",
    "collapse_citations_df",
]


@dataclass
class RunSummary:
    """Result of a batch or prefix run.

    Attributes
    ----------
    run_id : int
        Row ID of the run in the `runs` table.
    started_at, finished_at : datetime
        UTC timestamps bounding the run.
    inputs_total : int
        Number of input DOIs after normalization (and, for prefix runs, after
        enumeration). Excludes invalid inputs.
    inputs_skipped : int
        Number of (cited_doi, source) pairs skipped due to `refresh_after`.
    rows_per_source : dict[str, int]
        Rows written per source table during this run.
    errors_per_source : dict[str, str]
        Sources that errored (and a short description), if any.
    coalesced : bool
        Whether the `citations` table was rebuilt at the end of the run.
    citations_count : int | None
        If `coalesced`, the number of edges in `citations` after the rebuild.
    """

    run_id: int
    started_at: datetime
    finished_at: datetime
    inputs_total: int
    inputs_skipped: int
    rows_per_source: dict[str, int] = field(default_factory=dict)
    errors_per_source: dict[str, str] = field(default_factory=dict)
    coalesced: bool = False
    citations_count: int | None = None

    @property
    def total_rows_written(self) -> int:
        return sum(self.rows_per_source.values())


# ---- entry points -------------------------------------------------------

def find_citations(
    doi: str,
    *,
    db_path: str | Path | None = None,
    sources: Iterable[str] | str = "all",
    output: str = "long",
    email: str | None = None,
    api_keys: dict[str, str] | None = None,
    concurrency: int = 8,
    refresh_after: timedelta | None = None,
    force: bool = False,
    coalesce: bool = True,
    checkpoint_interval: int = 500,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord] | RunSummary:
    """Find citations of a single dataset DOI.

    Default behavior (no `db_path`): query and return an in-memory DataFrame
    (or grouped records). When `db_path` is given, also persist to that
    store and return a `RunSummary` instead.

    `api_keys` is a dict mapping source-id to key (e.g., {'openalex': '...'}).
    See `dataset_citation_finder.api_keys` for environment-variable fallbacks.
    """
    norm = normalize_doi(doi)
    if norm is None:
        raise ValueError(f"invalid DOI: {doi!r}")
    keys = resolve_api_keys(api_keys)

    if db_path is None:
        return _run_in_memory(
            dois=[norm], prefix=None, sources=sources, output=output,
            email=email, api_keys=keys, concurrency=concurrency, **adapter_opts,
        )
    return _run_to_store(
        db_path=db_path, dois=[norm], prefix=None, input_kind="single",
        input_value=norm, sources=sources, email=email, api_keys=keys,
        concurrency=concurrency, refresh_after=refresh_after, force=force,
        coalesce=coalesce, checkpoint_interval=checkpoint_interval,
        **adapter_opts,
    )


_BATCH_NO_DB_RECOMMEND_THRESHOLD = 100


def find_citations_batch(
    dois: Iterable[str],
    db_path: str | Path | None = None,
    *,
    sources: Iterable[str] | str = "all",
    output: str = "long",
    email: str | None = None,
    api_keys: dict[str, str] | None = None,
    concurrency: int = 8,
    refresh_after: timedelta | None = None,
    force: bool = False,
    coalesce: bool = True,
    checkpoint_interval: int = 500,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord] | RunSummary:
    """Find citations for an arbitrary list of dataset DOIs.

    Default behavior (no `db_path`): in-memory pipeline returning a
    long-format DataFrame (or grouped records). When `db_path` is given,
    persists to the store and returns a `RunSummary`.

    `api_keys` is a dict mapping source-id to key. See
    `dataset_citation_finder.api_keys` for environment-variable fallbacks.
    """
    normed: list[str] = []
    for d in dois:
        n = normalize_doi(d)
        if n is None:
            log.warning("skipping invalid DOI: %r", d)
            continue
        normed.append(n)
    if not normed:
        raise ValueError("no valid DOIs in input")
    keys = resolve_api_keys(api_keys)

    if db_path is None:
        if len(normed) > _BATCH_NO_DB_RECOMMEND_THRESHOLD:
            log.info(
                "find_citations_batch: %d DOIs without db_path; consider "
                "supplying db_path for resumability and the freshness filter",
                len(normed),
            )
        return _run_in_memory(
            dois=normed, prefix=None, sources=sources, output=output,
            email=email, api_keys=keys, concurrency=concurrency, **adapter_opts,
        )
    return _run_to_store(
        db_path=db_path, dois=normed, prefix=None, input_kind="batch",
        input_value=None, sources=sources, email=email, api_keys=keys,
        concurrency=concurrency, refresh_after=refresh_after, force=force,
        coalesce=coalesce, checkpoint_interval=checkpoint_interval,
        **adapter_opts,
    )


def find_citations_by_prefix(
    prefix: str,
    db_path: str | Path,
    *,
    sources: Iterable[str] | str = "all",
    email: str | None = None,
    api_keys: dict[str, str] | None = None,
    concurrency: int = 8,
    cache_dir: str | None = None,
    refresh_after: timedelta | None = None,
    force: bool = False,
    coalesce: bool = True,
    checkpoint_interval: int = 500,
    **adapter_opts: Any,
) -> RunSummary:
    """Find citations for every DOI under a DOI prefix.

    Writes results into the SQLite store at `db_path` and returns a
    `RunSummary`.

    `api_keys` is a dict mapping source-id to key. See
    `dataset_citation_finder.api_keys` for environment-variable fallbacks.
    """
    if db_path is None:
        raise TypeError("find_citations_by_prefix requires a db_path")
    prefix = prefix.lower().rstrip("/")
    keys = resolve_api_keys(api_keys)
    return _run_to_store(
        db_path=db_path, dois=None, prefix=prefix, input_kind="prefix",
        input_value=prefix, sources=sources, email=email, api_keys=keys,
        concurrency=concurrency, cache_dir=cache_dir,
        refresh_after=refresh_after, force=force, coalesce=coalesce,
        checkpoint_interval=checkpoint_interval, **adapter_opts,
    )


def validate_dois(
    dois: Iterable[str] | str,
    db_path: str | Path | None = None,
    *,
    email: str | None = None,
    refresh_after: timedelta | None = None,
    force: bool = False,
    check_registrar: bool = True,
    concurrency: int = 8,
) -> pd.DataFrame | RunSummary:
    """Validate that DOIs are registered and resolvable.

    Sends `HEAD https://doi.org/<doi>` per DOI; classifies each as
    'registered' (3xx redirect), 'unresolved' (404), or 'unknown' (other).
    When `check_registrar=True` (default), also probes DataCite and CrossRef
    metadata APIs to identify the registrar.

    Without `db_path`: returns a long-format DataFrame with one row per DOI.
    With `db_path`: writes results into `cited_dois` (denormalized latest
    status) and `doi_validations` (per-event audit log), and returns a
    `RunSummary` with `input_kind='validate'`. Honors `refresh_after`.

    See design doc §5.3 and §6.5.
    """
    if isinstance(dois, str):
        dois = [dois]
    normed: list[str] = []
    for d in dois:
        n = normalize_doi(d)
        if n is None:
            log.warning("validate_dois: skipping invalid DOI %r", d)
            continue
        normed.append(n)
    if not normed:
        raise ValueError("no valid DOIs in input")

    if db_path is None:
        async def _pipeline_mem():
            async with make_client(email=email) as client:
                return await validate_dois_async(
                    normed, client=client,
                    check_registrar=check_registrar,
                    concurrency=concurrency,
                )
        results = asyncio.run(_pipeline_mem())
        return pd.DataFrame.from_records(results, columns=[
            "cited_doi", "validation_status", "http_status",
            "validated_registrar", "notes", "validated_at",
        ])

    started_at = utcnow()
    conn = open_store(db_path)
    try:
        run_id = insert_run(
            conn,
            input_kind="validate",
            input_value=None,
            input_count=len(normed),
            sources_requested=["doi.org"]
                + (["datacite", "crossref"] if check_registrar else []),
        )

        # Pre-record the input set in cited_dois with origin='validation'
        # (a no-op upsert for DOIs already present from prior runs).
        upsert_cited_dois(
            conn, cited_dois=normed, origin="validation", run_id=run_id,
        )

        kept, skipped = filter_by_validation_freshness(
            conn, dois=normed, refresh_after=refresh_after, force=force,
        )

        async def _pipeline_db():
            if not kept:
                return []
            async with make_client(email=email) as client:
                return await validate_dois_async(
                    kept, client=client,
                    check_registrar=check_registrar,
                    concurrency=concurrency,
                )
        results = asyncio.run(_pipeline_db())

        # Persist inside an explicit transaction (open_store is autocommit).
        if results:
            conn.execute("BEGIN")
            try:
                persist_validation_results(conn, results, run_id=run_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        finished_at = utcnow()
        finish_run(
            conn, run_id,
            sources_succeeded=["doi.org"],
            rows_written=len(results),
            notes=None,
        )
        return RunSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            inputs_total=len(normed),
            inputs_skipped=skipped,
            rows_per_source={"doi.org": len(results)},
            errors_per_source={},
            coalesced=False,
            citations_count=None,
        )
    finally:
        conn.close()


# ---- store-side helpers (re-exports + thin wrappers) --------------------

def coalesce_citations(db_path: str | Path) -> int:
    """Rebuild the `citations` table from current source_* tables.

    Returns the row count of the rebuilt table.
    """
    return _coalesce_in_store(db_path)


def read_citations(
    db_path: str | Path,
    *,
    view: str = "coalesced",
) -> pd.DataFrame:
    """Load a store table as a DataFrame.

    Parameters
    ----------
    view : str
        'coalesced'      → the `citations` table (canonical edges).
        'long'           → UNION ALL of all source_* tables.
        'cited_dois'     → the in-scope DOI registry, including uncited DOIs.
        'doi_validations'→ append-only audit log of validation events.
        '<source_id>'    → that single source's raw table.
    """
    conn = open_store(db_path, create=False, migrate=False, read_only=True)
    try:
        if view == "coalesced":
            return pd.read_sql_query("SELECT * FROM citations", conn)
        if view == "long":
            parts = []
            for sid, table in SOURCE_TABLES.items():
                df = pd.read_sql_query(
                    f"SELECT cited_doi, citing_doi, '{sid}' AS source, "
                    f"relation_type, last_retrieved_at AS retrieved_at "
                    f"FROM {table}",
                    conn,
                )
                parts.append(df)
            if not parts:
                return pd.DataFrame(columns=[
                    "cited_doi", "citing_doi", "source", "relation_type", "retrieved_at"
                ])
            return pd.concat(parts, ignore_index=True)
        if view == "cited_dois":
            return pd.read_sql_query("SELECT * FROM cited_dois", conn)
        if view == "doi_validations":
            return pd.read_sql_query(
                "SELECT * FROM doi_validations ORDER BY validated_at", conn
            )
        if view in SOURCE_TABLES:
            return pd.read_sql_query(f"SELECT * FROM {SOURCE_TABLES[view]}", conn)
        raise ValueError(f"unknown view: {view!r}")
    finally:
        conn.close()


def export_citations(
    db_path: str | Path,
    output_path: str | Path,
    *,
    format: str = "csv",
    view: str = "coalesced",
) -> Path:
    """Export from the SQLite store to a flat file.

    `format` is 'csv' or 'parquet'; `view` is forwarded to `read_citations`.
    """
    df = read_citations(db_path, view=view)
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if format == "csv":
        df.to_csv(out, index=False)
    elif format == "parquet":
        df.to_parquet(out, index=False)
    else:
        raise ValueError(f"unknown export format: {format!r}")
    return out


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
    api_keys: dict[str, str] | None = None,
) -> list[SourceAdapter]:
    api_keys = api_keys or {}
    adapters: list[SourceAdapter] = []
    for sid in source_ids:
        cls = REGISTRY.get(sid)
        if cls is None:
            log.warning("unknown source %r; skipping", sid)
            continue
        effective_concurrency = min(concurrency, cls.default_concurrency)
        kwargs: dict[str, Any] = {"concurrency": effective_concurrency}
        if sid == "datacite_events" and "relation_types" in adapter_opts:
            kwargs["relation_types"] = adapter_opts["relation_types"]
        # Forward a resolved key only to adapters that authenticate. Others
        # (e.g. DataCite Events) silently ignore any entry in the dict.
        if cls.accepts_api_key and sid in api_keys:
            kwargs["api_key"] = api_keys[sid]
        adapters.append(cls(client, **kwargs))
    return adapters


def _observed_cited_dois(
    conn: sqlite3.Connection,
    run_id: int,
    adapters: list[SourceAdapter],
) -> set[str]:
    """Pull distinct cited_dois written by these adapters during this run.

    Used after a prefix-native query (e.g., DataCite Events on a prefix) to
    feed cited_dois with the DOIs the source surfaced.
    """
    out: set[str] = set()
    for adapter in adapters:
        table = SOURCE_TABLES.get(adapter.source_id)
        if not table:
            continue
        rows = conn.execute(
            f"SELECT DISTINCT cited_doi FROM {table} WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        out.update(r["cited_doi"] for r in rows)
    return out


def _filter_by_freshness(
    conn: sqlite3.Connection,
    *,
    dois: list[str],
    source: str,
    refresh_after: timedelta | None,
    force: bool,
) -> tuple[list[str], int]:
    """Return (kept, skipped_count) after freshness filtering.

    A pair is kept if `force` is True, `refresh_after` is None, the pair has
    never been queried, or its last_queried_at is older than now-refresh_after.
    """
    if force or refresh_after is None or not dois:
        return dois, 0
    cutoff = utcnow() - refresh_after
    cutoff_iso = cutoff.isoformat()
    last_seen = fetch_freshness(conn, cited_dois=dois, source=source)
    kept = [d for d in dois if last_seen.get(d, "") < cutoff_iso]
    return kept, len(dois) - len(kept)


def _run_in_memory(
    *,
    dois: list[str] | None,
    prefix: str | None,
    sources: Iterable[str] | str,
    output: str,
    email: str | None,
    concurrency: int,
    api_keys: dict[str, str] | None = None,
    cache_dir: str | None = None,
    **adapter_opts: Any,
) -> pd.DataFrame | list[GroupedRecord]:
    source_ids = _resolve_sources(sources)

    async def _pipeline():
        async with make_client(email=email) as client:
            adapters = _build_adapters(source_ids, client, concurrency, adapter_opts, api_keys=api_keys)
            if not adapters:
                raise ValueError("no adapters resolved from requested sources")
            if prefix is not None:
                non_prefix = [a for a in adapters if not a.supports_prefix]
                prefix_adapters = [a for a in adapters if a.supports_prefix]
                rows_p = await collect_rows(prefix_adapters, prefix=prefix) if prefix_adapters else []
                if non_prefix:
                    enumerator = PrefixEnumerator(client, cache_dir=cache_dir)
                    enumerated = await enumerator.enumerate(prefix)
                    rows_d = await collect_rows(non_prefix, dois=enumerated) if enumerated else []
                else:
                    rows_d = []
                rows = rows_p + rows_d
            else:
                rows = await collect_rows(adapters, dois=dois)
        return _shape_output(rows, output)

    return asyncio.run(_pipeline())


def _run_to_store(
    *,
    db_path: str | Path,
    dois: list[str] | None,
    prefix: str | None,
    input_kind: str,
    input_value: str | None,
    sources: Iterable[str] | str,
    email: str | None,
    concurrency: int,
    refresh_after: timedelta | None,
    force: bool,
    coalesce: bool,
    checkpoint_interval: int,
    api_keys: dict[str, str] | None = None,
    cache_dir: str | None = None,
    **adapter_opts: Any,
) -> RunSummary:
    source_ids = _resolve_sources(sources)
    started_at = utcnow()
    conn = open_store(db_path)
    try:
        # Determine input_count once we know the working set (for prefix this
        # requires enumeration; we fill input_count after that).
        run_id = insert_run(
            conn,
            input_kind=input_kind,
            input_value=input_value,
            input_count=(len(dois) if dois is not None else None),
            sources_requested=source_ids,
        )

        result = StoreCollectResult()
        inputs_total = 0
        inputs_skipped = 0

        # For non-prefix runs we know the input set up front; record it now
        # so DOIs that no source returns rows for are still preserved as
        # "in-scope" entries in cited_dois.
        if dois is not None:
            upsert_cited_dois(
                conn, cited_dois=dois, origin="input", run_id=run_id,
            )

        async def _pipeline():
            nonlocal inputs_total, inputs_skipped
            async with make_client(email=email) as client:
                adapters = _build_adapters(
                    source_ids, client, concurrency, adapter_opts, api_keys=api_keys,
                )
                if not adapters:
                    raise ValueError("no adapters resolved from requested sources")

                if prefix is not None:
                    prefix_adapters = [a for a in adapters if a.supports_prefix]
                    non_prefix = [a for a in adapters if not a.supports_prefix]

                    if prefix_adapters:
                        sub = await collect_to_store(
                            prefix_adapters, conn,
                            run_id=run_id, prefix=prefix,
                            checkpoint_interval=checkpoint_interval,
                        )
                        _merge_results(result, sub)
                        # Record any cited DOIs the prefix-native query
                        # surfaced (DataCite Events on prefix returns rows
                        # without our needing to enumerate first).
                        observed = _observed_cited_dois(conn, run_id, prefix_adapters)
                        if observed:
                            upsert_cited_dois(
                                conn, cited_dois=observed,
                                origin=f"prefix:{prefix}", run_id=run_id,
                            )

                    if non_prefix:
                        enumerator = PrefixEnumerator(client, cache_dir=cache_dir)
                        enumerated = await enumerator.enumerate(prefix)
                        log.info("prefix %s: %d DOIs enumerated", prefix, len(enumerated))
                        # Record the enumerated set in cited_dois — the
                        # exhaustive in-scope list, including DOIs that no
                        # adapter returns citations for.
                        if enumerated:
                            upsert_cited_dois(
                                conn, cited_dois=enumerated,
                                origin=f"prefix:{prefix}", run_id=run_id,
                            )
                        for adapter in non_prefix:
                            kept, skipped = _filter_by_freshness(
                                conn, dois=enumerated,
                                source=adapter.source_id,
                                refresh_after=refresh_after, force=force,
                            )
                            inputs_skipped += skipped
                            if not kept:
                                continue
                            sub = await collect_to_store(
                                [adapter], conn, run_id=run_id, dois=kept,
                                checkpoint_interval=checkpoint_interval,
                            )
                            _merge_results(result, sub)
                        inputs_total = len(enumerated)
                    else:
                        inputs_total = 0
                else:
                    inputs_total = len(dois or [])
                    for adapter in adapters:
                        kept, skipped = _filter_by_freshness(
                            conn, dois=dois or [],
                            source=adapter.source_id,
                            refresh_after=refresh_after, force=force,
                        )
                        inputs_skipped += skipped
                        if not kept:
                            continue
                        sub = await collect_to_store(
                            [adapter], conn, run_id=run_id, dois=kept,
                            checkpoint_interval=checkpoint_interval,
                        )
                        _merge_results(result, sub)

        asyncio.run(_pipeline())

        finished_at = utcnow()
        finish_run(
            conn, run_id,
            sources_succeeded=result.succeeded,
            rows_written=result.total_rows,
            notes=_format_notes(result.errors_per_source),
        )

        citations_count: int | None = None
        if coalesce:
            citations_count = _coalesce_in_store(conn)

        return RunSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            inputs_total=inputs_total,
            inputs_skipped=inputs_skipped,
            rows_per_source=dict(result.rows_per_source),
            errors_per_source=dict(result.errors_per_source),
            coalesced=coalesce,
            citations_count=citations_count,
        )
    finally:
        conn.close()


def _merge_results(into: StoreCollectResult, sub: StoreCollectResult) -> None:
    for k, v in sub.rows_per_source.items():
        into.rows_per_source[k] = into.rows_per_source.get(k, 0) + v
    for k, v in sub.errors_per_source.items():
        into.errors_per_source[k] = v
    for s in sub.succeeded:
        if s not in into.succeeded:
            into.succeeded.append(s)


def _format_notes(errors: dict[str, str]) -> str | None:
    if not errors:
        return None
    return "; ".join(f"{k}: {v}" for k, v in errors.items())


def _shape_output(rows, output: str):
    if output == "long":
        return rows_to_dataframe(rows)
    if output == "grouped":
        return rows_to_grouped(rows)
    raise ValueError(f"unknown output shape: {output!r}")
