"""SQLite store schema (DDL) and version constants.

A single `meta` row stamps the file with the schema version it was created at
and the library version that last touched it. Migrations live in
`store.migrations` as numbered modules; each bumps the version and is
idempotent. See design doc §7.2 and §7.4.
"""

from __future__ import annotations

# 0x44434646 = bytes 'DCFF' — written into PRAGMA application_id so we can
# detect whether a foreign SQLite file was produced by this library.
APPLICATION_ID = 0x44434646

# Bumped by every migration. Equal to the highest migration `to_version`.
CURRENT_SCHEMA_VERSION = 2


META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    schema_version   INTEGER NOT NULL,
    library_version  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT,
    application_id   INTEGER NOT NULL DEFAULT 1145258054   -- 0x44434646
);
"""

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    input_kind        TEXT NOT NULL,
    input_value       TEXT,
    input_count       INTEGER,
    sources_requested TEXT NOT NULL,
    sources_succeeded TEXT,
    rows_written      INTEGER,
    notes             TEXT
);
"""

QUERIED_DOIS_DDL = """
CREATE TABLE IF NOT EXISTS queried_dois (
    cited_doi        TEXT NOT NULL,
    source           TEXT NOT NULL,
    last_queried_at  TEXT NOT NULL,
    last_run_id      INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, source)
);
"""

SOURCE_DATACITE_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS source_datacite_events (
    event_id            TEXT PRIMARY KEY,
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    upstream_source     TEXT,
    occurred_at         TEXT,
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id)
);
"""

SOURCE_DATACITE_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_dce_pair
    ON source_datacite_events (cited_doi, citing_doi);
"""

SOURCE_OPENALEX_DDL = """
CREATE TABLE IF NOT EXISTS source_openalex (
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    citing_openalex_id  TEXT,
    cited_openalex_id   TEXT,
    relation_type       TEXT,
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, citing_doi)
);
"""

SOURCE_OPENCITATIONS_DDL = """
CREATE TABLE IF NOT EXISTS source_opencitations (
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    creation_date       TEXT,
    journal_sc          TEXT,
    author_sc           TEXT,
    relation_type       TEXT,
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, citing_doi)
);
"""

CITATIONS_DDL = """
CREATE TABLE IF NOT EXISTS citations (
    cited_doi          TEXT NOT NULL,
    citing_doi         TEXT NOT NULL,
    sources            TEXT NOT NULL,
    source_count       INTEGER NOT NULL,
    relation_types     TEXT,
    first_retrieved_at TEXT NOT NULL,
    last_retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (cited_doi, citing_doi)
);
"""

CITATIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_citations_citing
    ON citations (citing_doi);
"""

# v2 schema: cited_dois (in-scope DOI registry) and doi_validations (audit log).
# `cited_dois` records every DOI ever submitted as input or enumerated under a
# prefix, independent of whether any source has returned citations for it.
# Many rows here will have no matching rows in `citations` — that is the point:
# they are the dataset DOIs we know about but that are not (yet) cited.
CITED_DOIS_DDL = """
CREATE TABLE IF NOT EXISTS cited_dois (
    cited_doi            TEXT PRIMARY KEY,
    first_seen_at        TEXT NOT NULL,
    first_seen_run_id    INTEGER REFERENCES runs(run_id),
    last_seen_at         TEXT NOT NULL,
    last_seen_run_id     INTEGER REFERENCES runs(run_id),
    origin               TEXT NOT NULL,
    validation_status    TEXT,
    validated_at         TEXT,
    validated_registrar  TEXT
);
"""

DOI_VALIDATIONS_DDL = """
CREATE TABLE IF NOT EXISTS doi_validations (
    cited_doi    TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    method       TEXT NOT NULL,
    status       TEXT NOT NULL,
    http_status  INTEGER,
    registrar    TEXT,
    notes        TEXT,
    run_id       INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, validated_at, method)
);
"""

# Tables created at schema_version=1. Migrations beyond v1 add new tables
# (or alter existing ones) by appending to this list via the migrations
# subpackage rather than editing this file.
INITIAL_DDL_STATEMENTS: tuple[str, ...] = (
    META_DDL,
    RUNS_DDL,
    QUERIED_DOIS_DDL,
    SOURCE_DATACITE_EVENTS_DDL,
    SOURCE_DATACITE_EVENTS_INDEX,
    SOURCE_OPENALEX_DDL,
    SOURCE_OPENCITATIONS_DDL,
    CITATIONS_DDL,
    CITATIONS_INDEX,
)

# Map of source_id -> per-source raw table name. The coalesce step iterates
# this map; new adapters add an entry here (and a migration creating the table).
SOURCE_TABLES: dict[str, str] = {
    "datacite_events": "source_datacite_events",
    "openalex": "source_openalex",
    "opencitations": "source_opencitations",
}
