"""Optional SQLite store, for large DOI lists that are refreshed periodically.

Two tables:

    citations  one row per (cited_doi, citing_doi, source, relation_type),
               with when we first and last saw it
    queried    when each (cited_doi, source) pair was last successfully
               queried; this is what `refresh_after` checks

Results are committed after every DOI, so an interrupted run loses at most
one DOI's work and re-running picks up where it stopped.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

STORE_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS citations (
    cited_doi     TEXT NOT NULL,
    citing_doi    TEXT NOT NULL,
    source        TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (cited_doi, citing_doi, source, relation_type)
);
CREATE TABLE IF NOT EXISTS queried (
    cited_doi   TEXT NOT NULL,
    source      TEXT NOT NULL,
    queried_at  TEXT NOT NULL,
    PRIMARY KEY (cited_doi, source)
);
"""


class StoreError(Exception):
    """The file isn't a store this version of the library can use."""


def now() -> str:
    """Current UTC time as an ISO 8601 string (the store's timestamp format)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_store(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    path = Path(path).expanduser()
    if not create and not path.exists():
        raise FileNotFoundError(path)
    # Each source writes from its own thread, so wait for others' writes.
    conn = sqlite3.connect(path, timeout=60)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
    if version == 0 and not has_tables:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
    elif version != STORE_VERSION:
        conn.close()
        raise StoreError(
            f"{path} was not created by this version of dataset-citation-finder "
            f"(store version {version}, expected {STORE_VERSION}). "
            "Use a new db_path, or delete the file and re-run."
        )
    return conn


def dois_to_query(conn: sqlite3.Connection, dois: list[str], source: str,
                  refresh_after: timedelta | None) -> list[str]:
    """Drop DOIs that `source` answered within the last `refresh_after`."""
    if refresh_after is None:
        return dois
    cutoff = (datetime.now(timezone.utc) - refresh_after).isoformat(timespec="seconds")
    recent = {doi for (doi,) in conn.execute(
        "SELECT cited_doi FROM queried WHERE source = ? AND queried_at >= ?",
        (source, cutoff),
    )}
    return [d for d in dois if d not in recent]


def save(conn: sqlite3.Connection, source: str, doi: str, rows: list[dict]) -> None:
    """Record one DOI's results from one source, and mark it queried."""
    ts = now()
    with conn:  # one transaction, committed on exit
        conn.executemany(
            "INSERT INTO citations VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (cited_doi, citing_doi, source, relation_type) "
            "DO UPDATE SET last_seen = excluded.last_seen",
            [(r["cited_doi"], r["citing_doi"], r["source"], r["relation_type"], ts, ts)
             for r in rows],
        )
        conn.execute(
            "INSERT INTO queried VALUES (?, ?, ?) "
            "ON CONFLICT (cited_doi, source) DO UPDATE SET queried_at = excluded.queried_at",
            (doi, source, ts),
        )


def read_rows(conn: sqlite3.Connection) -> pd.DataFrame:
    """Every stored row, one per (cited_doi, citing_doi, source, relation_type)."""
    return pd.read_sql_query("SELECT * FROM citations", conn)
