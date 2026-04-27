"""Adapter orchestration.

Two collection paths:

1. `collect_rows` — accumulates LongRow's in memory. Used by `find_citations`
   when no `db_path` is provided (the single-DOI convenience case).

2. `collect_to_store` — streams adapter output directly into the per-source
   tables of an open SQLite store via the upsert helpers. Used by
   `find_citations_batch`, `find_citations_by_prefix`, and `find_citations`
   when a `db_path` is provided.

Plus utilities to convert in-memory rows to either a long-format DataFrame
(`rows_to_dataframe`) or grouped records (`rows_to_grouped`).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from .schema import GroupedRecord, LongRow
from .store.upsert import bump_queried_dois, UPSERTERS

if TYPE_CHECKING:
    from .adapters.base import SourceAdapter

log = logging.getLogger(__name__)


# ---- in-memory path -----------------------------------------------------

async def collect_rows(
    adapters: list["SourceAdapter"],
    *,
    dois: Iterable[str] | None = None,
    prefix: str | None = None,
) -> list[LongRow]:
    """Run all adapters and collect their LongRow outputs into a list.

    Exactly one of `dois` or `prefix` should be provided.
    """
    if (dois is None) == (prefix is None):
        raise ValueError("provide exactly one of `dois` or `prefix`")

    dois_list = list(dois) if dois is not None else None

    async def _run(adapter: "SourceAdapter") -> list[LongRow]:
        rows: list[LongRow] = []
        try:
            if prefix is not None and adapter.supports_prefix:
                async for row in adapter.query_prefix(prefix):
                    rows.append(row)
            elif prefix is not None:
                log.debug("%s: skipping prefix path (not supported)", adapter.source_id)
            else:
                async for row in adapter.query_batch(dois_list or []):
                    rows.append(row)
        except NotImplementedError as e:
            log.warning("%s: not implemented, skipping (%s)", adapter.source_id, e)
        except Exception:
            log.exception("%s: adapter failed", adapter.source_id)
        return rows

    results = await asyncio.gather(*(_run(a) for a in adapters))
    return [row for adapter_rows in results for row in adapter_rows]


# ---- store path ---------------------------------------------------------

@dataclass
class StoreCollectResult:
    rows_per_source: dict[str, int] = field(default_factory=dict)
    errors_per_source: dict[str, str] = field(default_factory=dict)
    succeeded: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_per_source.values())


async def collect_to_store(
    adapters: list["SourceAdapter"],
    conn: sqlite3.Connection,
    *,
    run_id: int,
    dois: Iterable[str] | None = None,
    prefix: str | None = None,
    checkpoint_interval: int = 500,
) -> StoreCollectResult:
    """Stream adapter output into the store, upserting per-source.

    Adapters run in parallel; each upserts into its own table, so writes
    don't contend on the same row. The connection is opened in autocommit
    mode by `open_store`; we run one BEGIN at start and COMMIT periodically
    based on `checkpoint_interval` (counted across all adapters).

    For per-DOI / per-batch adapters, `queried_dois` is bumped for every DOI
    in the input list (whether or not the adapter returned rows for it).
    For prefix adapters, `queried_dois` is bumped for every cited_doi
    observed in the adapter's output.
    """
    if (dois is None) == (prefix is None):
        raise ValueError("provide exactly one of `dois` or `prefix`")

    dois_list = list(dois) if dois is not None else None
    result = StoreCollectResult()
    rows_since_commit = 0

    def _checkpoint_if_needed() -> None:
        nonlocal rows_since_commit
        if rows_since_commit >= checkpoint_interval:
            conn.execute("COMMIT")
            conn.execute("BEGIN")
            rows_since_commit = 0

    conn.execute("BEGIN")

    async def _run(adapter: "SourceAdapter") -> None:
        nonlocal rows_since_commit
        sid = adapter.source_id
        upserter = UPSERTERS.get(sid)
        if upserter is None:
            log.warning("%s: no upsert registered for source; skipping store path", sid)
            return

        rows_written = 0
        observed_cited: set[str] = set()
        try:
            if prefix is not None and adapter.supports_prefix:
                stream = adapter.query_prefix(prefix)
            elif prefix is not None:
                log.debug("%s: skipping prefix path (not supported)", sid)
                return
            else:
                stream = adapter.query_batch(dois_list or [])

            async for row in stream:
                upserter(conn, row, run_id)
                observed_cited.add(row.cited_doi)
                rows_written += 1
                rows_since_commit += 1
                _checkpoint_if_needed()
        except NotImplementedError as e:
            log.warning("%s: not implemented, skipping (%s)", sid, e)
            result.errors_per_source[sid] = f"not implemented: {e}"
            return
        except Exception as e:
            log.exception("%s: adapter failed", sid)
            result.errors_per_source[sid] = repr(e)
            # Don't return — record what we did write.

        if dois_list is not None:
            bump_queried_dois(conn, cited_dois=dois_list, source=sid, run_id=run_id)
        elif observed_cited:
            bump_queried_dois(conn, cited_dois=observed_cited, source=sid, run_id=run_id)

        result.rows_per_source[sid] = rows_written
        if sid not in result.errors_per_source:
            result.succeeded.append(sid)

    try:
        await asyncio.gather(*(_run(a) for a in adapters))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


# ---- in-memory shaping --------------------------------------------------

def rows_to_dataframe(rows: Iterable[LongRow]) -> pd.DataFrame:
    """Convert LongRow instances into a long-format pandas DataFrame.

    Columns: cited_doi, citing_doi, source, relation_type, retrieved_at.
    """
    records = [
        {
            "cited_doi": r.cited_doi,
            "citing_doi": r.citing_doi,
            "source": r.source,
            "relation_type": r.relation_type,
            "retrieved_at": r.retrieved_at,
        }
        for r in rows
    ]
    df = pd.DataFrame.from_records(records, columns=[
        "cited_doi", "citing_doi", "source", "relation_type", "retrieved_at"
    ])
    return df


def rows_to_grouped(rows: Iterable[LongRow]) -> list[GroupedRecord]:
    """Merge LongRow's into GroupedRecord's keyed by citing DOI."""
    by_citing: dict[str, GroupedRecord] = {}

    for row in rows:
        rec = by_citing.get(row.citing_doi)
        if rec is None:
            rec = GroupedRecord(doi=row.citing_doi)
            by_citing[row.citing_doi] = rec

        if row.source not in rec.sources:
            rec.sources.append(row.source)
        if row.cited_doi not in rec.cited_dois:
            rec.cited_dois.append(row.cited_doi)

        if row.source == "datacite_events":
            eid = row.extra.get("event_id")
            if eid and eid not in rec.datacite_event_ids:
                rec.datacite_event_ids.append(eid)
            if row.relation_type and row.relation_type not in rec.datacite_relation_types:
                rec.datacite_relation_types.append(row.relation_type)
        elif row.source == "openalex":
            if not rec.openalex_id:
                rec.openalex_id = row.extra.get("openalex_id")

        if not rec.title:
            rec.title = row.extra.get("title")
        if not rec.year:
            year = row.extra.get("year")
            if isinstance(year, int):
                rec.year = year

    return list(by_citing.values())
