"""Coalesce per-source raw tables into the canonical `citations` table.

For each adapter table listed in `SOURCE_TABLES`, we union (cited_doi,
citing_doi, source, relation_type, first_retrieved_at, last_retrieved_at)
into a single virtual long table, then GROUP BY the edge to produce one
row per (cited_doi, citing_doi).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

from .connection import open_store
from .schema import SOURCE_TABLES

log = logging.getLogger(__name__)


def coalesce_citations(db_path: str | Path | sqlite3.Connection) -> int:
    """Rebuild the `citations` table from the source_* tables.

    Accepts either a path (we open + close internally) or an already-open
    connection (we don't close it).

    Returns the number of rows written to `citations`.
    """
    own_conn = not isinstance(db_path, sqlite3.Connection)
    conn = open_store(db_path) if own_conn else db_path
    try:
        return _coalesce_in_place(conn)
    finally:
        if own_conn:
            conn.close()


def _coalesce_in_place(conn: sqlite3.Connection) -> int:
    """Inner coalesce: assumes conn is open and writable."""
    # Collect: for each table, pull the rows we need.
    edges: dict[
        tuple[str, str],
        dict,
    ] = defaultdict(
        lambda: {"sources": [], "relation_types": [], "first": None, "last": None}
    )

    for source_id, table in SOURCE_TABLES.items():
        rows = conn.execute(
            f"SELECT cited_doi, citing_doi, relation_type, "
            f"       first_retrieved_at, last_retrieved_at "
            f"FROM {table}"
        ).fetchall()
        for r in rows:
            key = (r["cited_doi"], r["citing_doi"])
            entry = edges[key]
            if source_id not in entry["sources"]:
                entry["sources"].append(source_id)
            rt = r["relation_type"]
            if rt and rt not in entry["relation_types"]:
                entry["relation_types"].append(rt)
            first = r["first_retrieved_at"]
            last = r["last_retrieved_at"]
            if entry["first"] is None or (first is not None and first < entry["first"]):
                entry["first"] = first
            if entry["last"] is None or (last is not None and last > entry["last"]):
                entry["last"] = last

    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM citations")
        if edges:
            conn.executemany(
                "INSERT INTO citations "
                "(cited_doi, citing_doi, sources, source_count, relation_types, "
                " first_retrieved_at, last_retrieved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        cited,
                        citing,
                        json.dumps(e["sources"]),
                        len(e["sources"]),
                        json.dumps(e["relation_types"]) if e["relation_types"] else None,
                        e["first"],
                        e["last"],
                    )
                    for (cited, citing), e in edges.items()
                ],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(edges)
