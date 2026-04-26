"""Merge/dedup engine.

Takes the stream of LongRow's produced by adapters and produces either:
- a pandas DataFrame in long format (one row per source attribution), or
- a list of GroupedRecord's (one per unique citing work, merged across sources).

The grouped-record path does DOI-first merging; when a citing work has no
DOI, it's kept as a separate record (fuzzy title/author matching is a future
enhancement — see design §4.2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING

import pandas as pd

from .schema import GroupedRecord, LongRow

if TYPE_CHECKING:
    from .adapters.base import SourceAdapter

log = logging.getLogger(__name__)


async def collect_rows(
    adapters: list["SourceAdapter"],
    *,
    dois: Iterable[str] | None = None,
    prefix: str | None = None,
) -> list[LongRow]:
    """Run all adapters and collect their LongRow outputs.

    Exactly one of `dois` or `prefix` should be provided.

    Dispatch per adapter:
        - If `prefix` is given and the adapter supports prefix → query_prefix
        - Else if `dois` is given → query_batch (adapter's default fans out
          over query_single with bounded concurrency)
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
                # Adapter doesn't support prefix; caller is expected to have
                # enumerated DOIs and passed them via `dois`. Skip quietly.
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


def rows_to_dataframe(rows: Iterable[LongRow]) -> pd.DataFrame:
    """Convert LongRow instances into a long-format pandas DataFrame.

    Columns: cited_doi, citing_doi, source, relation_type, retrieved_at.
    (The `extra` dict is dropped; it's available on LongRow instances for
    callers who want it but not included in the canonical table.)
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
    """Merge LongRow's into GroupedRecord's keyed by citing DOI.

    Each GroupedRecord represents one unique citing work. The `sources`,
    `cited_dois`, `datacite_event_ids`, and `datacite_relation_types` fields
    are populated by aggregation across the rows that share a `citing_doi`.

    Title/year/authors come from adapter `extra` when available; conflicting
    values across sources resolve to "first non-empty seen" (fuzzy merging
    is deferred per design §4.2).
    """
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

        # Adapter-specific provenance.
        if row.source == "datacite_events":
            eid = row.extra.get("event_id")
            if eid and eid not in rec.datacite_event_ids:
                rec.datacite_event_ids.append(eid)
            if row.relation_type and row.relation_type not in rec.datacite_relation_types:
                rec.datacite_relation_types.append(row.relation_type)
        elif row.source == "openalex":
            if not rec.openalex_id:
                rec.openalex_id = row.extra.get("openalex_id")

        # Generic fields from extras (first-non-empty-wins).
        if not rec.title:
            rec.title = row.extra.get("title")
        if not rec.year:
            year = row.extra.get("year")
            if isinstance(year, int):
                rec.year = year

    return list(by_citing.values())
