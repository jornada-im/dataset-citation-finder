"""Per-source upsert helpers and run/queried_dois bookkeeping.

Every helper expects an open sqlite3.Connection (typically from `open_store`).
All ON CONFLICT clauses preserve `first_retrieved_at` and advance only
`last_retrieved_at` and `run_id`. See design doc §7.3.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from ..schema import LongRow

log = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _ts(value: Any) -> str:
    """Coerce a datetime-or-string into an ISO 8601 string."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, str):
        return value
    return _utcnow_iso()


# ---- runs ---------------------------------------------------------------

def insert_run(
    conn: sqlite3.Connection,
    *,
    input_kind: str,
    input_value: str | None,
    input_count: int | None,
    sources_requested: Iterable[str],
) -> int:
    """Open a new run row. Returns its run_id."""
    cur = conn.execute(
        "INSERT INTO runs (started_at, input_kind, input_value, input_count, "
        "sources_requested) VALUES (?, ?, ?, ?, ?)",
        (
            _utcnow_iso(),
            input_kind,
            input_value,
            input_count,
            json.dumps(list(sources_requested)),
        ),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    sources_succeeded: Iterable[str],
    rows_written: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, sources_succeeded = ?, "
        "rows_written = ?, notes = ? WHERE run_id = ?",
        (
            _utcnow_iso(),
            json.dumps(list(sources_succeeded)),
            rows_written,
            notes,
            run_id,
        ),
    )


# ---- cited_dois ---------------------------------------------------------

def upsert_cited_dois(
    conn: sqlite3.Connection,
    *,
    cited_dois: Iterable[str],
    origin: str,
    run_id: int,
) -> None:
    """Record DOIs as in-scope. Preserves first_seen_at on conflict.

    `origin` is one of 'input' (explicit DOI list), 'prefix:<prefix>'
    (enumerated under a prefix), or 'validation' (added by validate_dois
    against an externally-supplied list).
    """
    now = _utcnow_iso()
    conn.executemany(
        "INSERT INTO cited_dois ("
        "  cited_doi, first_seen_at, first_seen_run_id, "
        "  last_seen_at, last_seen_run_id, origin"
        ") VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cited_doi) DO UPDATE SET "
        "  last_seen_at = excluded.last_seen_at, "
        "  last_seen_run_id = excluded.last_seen_run_id",
        [(d, now, run_id, now, run_id, origin) for d in cited_dois],
    )


def set_validation_status(
    conn: sqlite3.Connection,
    *,
    cited_doi: str,
    status: str,
    validated_at: str,
    registrar: str | None,
) -> None:
    """Update the denormalized validation columns on `cited_dois`.

    Inserts a row with origin='validation' if the DOI isn't already there
    (so validate_dois can be called against an arbitrary list without first
    routing the DOIs through the citation pipeline).
    """
    conn.execute(
        "INSERT INTO cited_dois ("
        "  cited_doi, first_seen_at, last_seen_at, origin, "
        "  validation_status, validated_at, validated_registrar"
        ") VALUES (?, ?, ?, 'validation', ?, ?, ?) "
        "ON CONFLICT(cited_doi) DO UPDATE SET "
        "  validation_status   = excluded.validation_status, "
        "  validated_at        = excluded.validated_at, "
        "  validated_registrar = excluded.validated_registrar",
        (cited_doi, validated_at, validated_at, status, validated_at, registrar),
    )


def append_doi_validation(
    conn: sqlite3.Connection,
    *,
    cited_doi: str,
    validated_at: str,
    method: str,
    status: str,
    http_status: int | None,
    registrar: str | None,
    notes: str | None,
    run_id: int | None,
) -> None:
    """Append-only audit row for a single validation event."""
    conn.execute(
        "INSERT OR REPLACE INTO doi_validations ("
        "  cited_doi, validated_at, method, status, http_status, "
        "  registrar, notes, run_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cited_doi, validated_at, method, status, http_status,
         registrar, notes, run_id),
    )


def fetch_validation_freshness(
    conn: sqlite3.Connection,
    *,
    cited_dois: Iterable[str],
) -> dict[str, str]:
    """Return {cited_doi: validated_at} for DOIs already validated.

    DOIs without a `validated_at` timestamp are absent from the result.
    """
    cited_dois = list(cited_dois)
    if not cited_dois:
        return {}
    out: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(cited_dois), chunk):
        batch = cited_dois[i : i + chunk]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT cited_doi, validated_at FROM cited_dois "
            f"WHERE validated_at IS NOT NULL "
            f"AND cited_doi IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        for r in rows:
            out[r["cited_doi"]] = r["validated_at"]
    return out


# ---- queried_dois -------------------------------------------------------

def bump_queried_dois(
    conn: sqlite3.Connection,
    *,
    cited_dois: Iterable[str],
    source: str,
    run_id: int,
) -> None:
    """Record that we just queried these (cited_doi, source) pairs."""
    now = _utcnow_iso()
    conn.executemany(
        "INSERT INTO queried_dois (cited_doi, source, last_queried_at, last_run_id) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(cited_doi, source) DO UPDATE SET "
        "  last_queried_at = excluded.last_queried_at, "
        "  last_run_id = excluded.last_run_id",
        [(d, source, now, run_id) for d in cited_dois],
    )


def fetch_freshness(
    conn: sqlite3.Connection,
    *,
    cited_dois: Iterable[str],
    source: str,
) -> dict[str, str]:
    """Return {cited_doi: last_queried_at_iso} for the requested DOIs.

    DOIs not present in `queried_dois` are absent from the result.
    """
    cited_dois = list(cited_dois)
    if not cited_dois:
        return {}
    # SQLite parameter-limit-safe chunking.
    out: dict[str, str] = {}
    chunk = 500
    for i in range(0, len(cited_dois), chunk):
        batch = cited_dois[i : i + chunk]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT cited_doi, last_queried_at FROM queried_dois "
            f"WHERE source = ? AND cited_doi IN ({placeholders})",
            (source, *batch),
        ).fetchall()
        for r in rows:
            out[r["cited_doi"]] = r["last_queried_at"]
    return out


# ---- per-source upserts -------------------------------------------------

def upsert_datacite_event(conn: sqlite3.Connection, row: LongRow, run_id: int) -> None:
    """Upsert a single DataCite event row.

    PK is event_id (events have stable IDs). Rows lacking an event_id are
    rejected — events without IDs would not survive an idempotent upsert.
    """
    event_id = row.extra.get("event_id")
    if not event_id:
        log.debug("datacite_events: skipping row with no event_id")
        return
    ts = _ts(row.retrieved_at)
    conn.execute(
        "INSERT INTO source_datacite_events ("
        "  event_id, cited_doi, citing_doi, relation_type, upstream_source, "
        "  occurred_at, first_retrieved_at, last_retrieved_at, run_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET "
        "  last_retrieved_at = excluded.last_retrieved_at, "
        "  run_id = excluded.run_id",
        (
            event_id,
            row.cited_doi,
            row.citing_doi,
            row.relation_type,
            row.extra.get("upstream_source"),
            row.extra.get("occurred_at"),
            ts,
            ts,
            run_id,
        ),
    )


def upsert_openalex(conn: sqlite3.Connection, row: LongRow, run_id: int) -> None:
    """Upsert a single OpenAlex row. PK is (cited_doi, citing_doi)."""
    ts = _ts(row.retrieved_at)
    conn.execute(
        "INSERT INTO source_openalex ("
        "  cited_doi, citing_doi, citing_openalex_id, cited_openalex_id, "
        "  relation_type, first_retrieved_at, last_retrieved_at, run_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cited_doi, citing_doi) DO UPDATE SET "
        "  citing_openalex_id = COALESCE(excluded.citing_openalex_id, source_openalex.citing_openalex_id), "
        "  cited_openalex_id  = COALESCE(excluded.cited_openalex_id,  source_openalex.cited_openalex_id), "
        "  last_retrieved_at  = excluded.last_retrieved_at, "
        "  run_id             = excluded.run_id",
        (
            row.cited_doi,
            row.citing_doi,
            row.extra.get("openalex_id"),
            row.extra.get("cited_openalex_id"),
            row.relation_type,
            ts,
            ts,
            run_id,
        ),
    )


def upsert_opencitations(conn: sqlite3.Connection, row: LongRow, run_id: int) -> None:
    """Upsert a single OpenCitations row. PK is (cited_doi, citing_doi)."""
    ts = _ts(row.retrieved_at)
    conn.execute(
        "INSERT INTO source_opencitations ("
        "  cited_doi, citing_doi, creation_date, journal_sc, author_sc, "
        "  relation_type, first_retrieved_at, last_retrieved_at, run_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cited_doi, citing_doi) DO UPDATE SET "
        "  creation_date     = COALESCE(excluded.creation_date, source_opencitations.creation_date), "
        "  journal_sc        = COALESCE(excluded.journal_sc,    source_opencitations.journal_sc), "
        "  author_sc         = COALESCE(excluded.author_sc,     source_opencitations.author_sc), "
        "  last_retrieved_at = excluded.last_retrieved_at, "
        "  run_id            = excluded.run_id",
        (
            row.cited_doi,
            row.citing_doi,
            row.extra.get("creation"),
            row.extra.get("journal_sc"),
            row.extra.get("author_sc"),
            row.relation_type,
            ts,
            ts,
            run_id,
        ),
    )


# Dispatch table consumed by engine.collect_to_store. New adapters add an
# entry here (and a per-source upsert function above).
UPSERTERS = {
    "datacite_events": upsert_datacite_event,
    "openalex": upsert_openalex,
    "opencitations": upsert_opencitations,
}
