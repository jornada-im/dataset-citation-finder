"""collapse_sources(): long-format DataFrame -> deduplicated per-edge view.

See design doc §7. Groups on (cited_doi, citing_doi) and produces:
    - sources: list[str] (unique, preserving first-seen order)
    - source_count: int
    - relation_types: list[str] (unique, preserving first-seen order)
    - first_retrieved_at: earliest retrieved_at across rows
"""

from __future__ import annotations

import pandas as pd


def _unique_preserve_order(values) -> list:
    seen = set()
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def collapse_sources(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the long-format table to one row per (cited_doi, citing_doi).

    Parameters
    ----------
    df : pd.DataFrame
        Long-format table with columns cited_doi, citing_doi, source,
        relation_type, retrieved_at.

    Returns
    -------
    pd.DataFrame
        One row per unique (cited_doi, citing_doi) edge, with columns
        cited_doi, citing_doi, sources, source_count, relation_types,
        first_retrieved_at.
    """
    required = {"cited_doi", "citing_doi", "source"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")

    if df.empty:
        return pd.DataFrame(columns=[
            "cited_doi", "citing_doi", "sources", "source_count",
            "relation_types", "first_retrieved_at",
        ])

    grouped = df.groupby(["cited_doi", "citing_doi"], sort=False, as_index=False).agg(
        sources=("source", _unique_preserve_order),
        relation_types=("relation_type", _unique_preserve_order) if "relation_type" in df.columns else ("source", lambda s: []),
        first_retrieved_at=("retrieved_at", "min") if "retrieved_at" in df.columns else ("source", lambda s: None),
    )
    grouped["source_count"] = grouped["sources"].map(len)
    # Reorder columns.
    return grouped[[
        "cited_doi", "citing_doi", "sources", "source_count",
        "relation_types", "first_retrieved_at",
    ]]
