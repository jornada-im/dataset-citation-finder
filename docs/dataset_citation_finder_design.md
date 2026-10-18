# Dataset Citation Finder — Design & Planning Document

*Single consolidated design document. Supersedes the earlier `.docx` design.*

> **Historical (v0.3).** v0.4 simplified the library: no asyncio, no adapter
> classes or migrations, no `find_citations_batch` / `find_citations_by_prefix`
> / `export_citations`, and a two-table optional store. The reasoning about
> sources, relation types and provenance below still applies. For the current
> design, see `README.md` and `CLAUDE.md`.

## 1. Overview

The **Dataset Citation Finder** is a Python library for discovering academic papers that cite a given dataset. It accepts a full DOI, a list of DOIs, or a DOI prefix (e.g., `10.6073/*` to sweep an entire dataset family such as the Environmental Data Initiative's catalog); queries multiple scholarly and citation-graph services; and persists results into a **SQLite database** with per-source provenance preserved.

The tool is designed as a Python library first — not a CLI or web service — and is repository-agnostic: dataset DOIs from any DataCite- or CrossRef-registered repository are valid input.

The intended workflow for non-trivial scopes (a repository, a research program, an institution's data holdings) is **populate-once, refresh-periodically**: an initial run materializes a table of enumerated DOIs to query, per-source raw tables and a collapsed citation edges table; subsequent runs against the same SQLite file may enumerate newly-minted DOIs to query and incrementally add citations for DOIs in the query list. Single-DOI lookups remain available as an in-memory convenience that does not require a database, but use of a database store is optional for batch lookups and required for prefix lookups.

## 2. Goals

- Accept input as (a) a single dataset DOI, (b) an arbitrary iterable of DOIs, or (c) a DOI prefix.
- Query multiple citation sources (see §4) and persist results into a SQLite database keyed for incremental updates.
- Allow an optional validation step to ensure that input DOIs are registered and resolveable. Do this as a function that operates separately from querying sources for citations.
- Preserve per-source provenance on every record — provenance is a first-class concern, not an afterthought. Every adapter writes to its own raw table; a collapsed canonical-edges table is a derivation.
- Support periodic refresh: re-running the same input against the same database queries only DOI/source pairs that haven't been updated within a configurable freshness window.
- **Operate transparently on a user-supplied database file previously produced by the tool.** The same `db_path` parameter both creates a new store and updates an existing one — there is no separate "init" vs "update" mode. Schema versioning and idempotent migrations make it safe to point a newer version of the library at an older store.
- Produce derivable exports in CSV, Parquet, BibTeX, and JSON from the SQLite store.
- Run efficiently at scale: a single-DOI lookup and a full-repository sweep (tens of thousands of DOIs) are the same pipeline, with automatic path selection.

## 3. Non-Goals

- Does not crawl full-text of papers to find informal dataset mentions.
- No R package or CLI interface in v1 (possible future work).

## 4. Architecture

Four layers: **source adapters → SQLite store → collapse citations → exporters.**

Each adapter implements a common interface: given a DOI (or batch of DOIs), yield a sequence of `LongRow` records. The engine writes those rows directly into a per-source table in the SQLite database (one table per adapter). After population — automatically at end of run, or on demand via `collapse_citations()` — a collapse step builds a canonical `citations` table: one row per unique `(cited_doi, citing_doi)` edge with `sources` and `relation_types` aggregated as JSON arrays. Exporters read from the store and emit CSV, Parquet, BibTeX, or JSON.

For single-DOI and batch lookups (`find_citations` and `find_citations_batch`), the database is optional; in its absence the engine returns an in-memory long-format pandas DataFrame, exactly as today. For batch lookups with over 100 DOIs, use of a database will be recommended to the user.

### 4.1 Source Adapters

| Adapter | API type | Rate limits | Auth | Batch / prefix support |
|---|---|---|---|---|
| OpenAlex | REST (free) | ~10 req/s (polite pool) | Email recommended | Per-DOI (concurrent) |
| Semantic Scholar | REST (free) | 1 req/s unauth; 10/s w/ key | Optional API key | Per-DOI (concurrent) |
| CrossRef | REST (free) | Polite pool w/ email | None | Per-DOI (concurrent) |
| DataCite Event Data | REST (free) | Polite pool | None | **Native prefix query** |
| OpenCitations (Index v2) | REST (free) | Polite pool | None | Per-DOI (concurrent) |
| Dimensions | DSL over REST | Per-account | **Required** (token or API key) | **Native batch** (list-valued DSL) |
| Google Scholar | Scraping | Low; backoff required | None (IP-limited) | Per-DOI; opt-in only |

**Defaults.** OpenAlex, Semantic Scholar, CrossRef, DataCite Event Data, and OpenCitations are enabled by default. Dimensions is auto-enabled when credentials are detected. Google Scholar is retired from the default set — remains in the codebase as opt-in due to reliability and legal concerns.

**Why this mix.** OpenAlex, Semantic Scholar, and CrossRef are general scholarly graphs. DataCite Event Data is the authoritative source for dataset-DOI citation events. OpenCitations aggregates CrossRef, DataCite, OpenAIRE, NIH-OCC, and JaLC — substantial overlap with other adapters is expected and intentional: provenance is preserved, and cross-source agreement becomes a signal rather than noise. Dimensions treats datasets as first-class linked entities and often surfaces edges the others miss.

### 4.2 Store and collapse

The SQLite store is the system of record. Each adapter writes to its own raw table (§7.2), preserving every observation including upstream IDs, timestamps, and source-specific metadata. Re-running a query upserts into the raw table on the table's primary key — duplicate rows are not created; `last_retrieved_at` is bumped instead, and `first_retrieved_at` is preserved.

A separate collapse step (run automatically after each batch/prefix run unless disabled, or invoked manually via `collapse_citations()`) rebuilds the `citations` table by unioning all `source_*` tables on `(cited_doi, citing_doi)`. The resulting table aggregates `sources` and `relation_types` as JSON arrays, plus `source_count`, `first_retrieved_at`, and `last_retrieved_at`. It is logically equivalent to the `collapse_citations_df(df)` output, but materialized and queryable.

When a citing work lacks a DOI, the row is dropped (DOI is required as the dedup key). Fuzzy title matching as a secondary key is deferred — see §14.

### 4.3 Exporters

Exporters read from the SQLite store and emit:

- **CSV / Parquet**: tabular export from either the collapsed `citations` table, the long-format union of per-source tables, or any single `source_*` table. Parquet preferred for large runs.
- **BibTeX**: one `@article` entry per collapsed citation; non-standard fields (`sources`, upstream IDs) emitted as `note` fields.
- **JSON**: list of grouped citation records (the collapsed shape).

Exporters are pure read-out functions over the store; they never query upstream services.

## 5. DOI Handling

### 5.1 Normalization

A single `normalize_doi()` in `utils` is called by every adapter before emitting records:

1. Strip URL prefixes: `https://doi.org/`, `http://dx.doi.org/`, `doi:`
2. Lowercase (DOIs are case-insensitive by spec)
3. URL-decode percent-encoded characters
4. Trim whitespace
5. Validate against `^10\.\d{4,9}/.+$`; log and drop malformed DOIs

Normalization at the adapter boundary ensures dedup keys work regardless of upstream formatting.

### 5.2 Prefix Expansion

Two query patterns are supported across adapters:

- **Prefix-capable sources** (DataCite Events): query by `obj-id.prefix=<prefix>` directly; no DOI enumeration needed. This is the most efficient path for repository-wide sweeps.
- **Per-DOI sources** (everything else): require a concrete DOI list.

A shared `PrefixEnumerator` component fetches the full DOI list from DataCite:

```
GET https://api.datacite.org/dois?query=prefix:<prefix>
                                 &page[size]=1000
                                 &page[cursor]=...
```

Results are cached on disk (default TTL 24 h) for non-database queries and written to the `cited_dois` table of the SQLite store (see §5.3 and §7.2 for that table's purpose).

### 5.3 Optional DOI validation

`normalize_doi` only confirms DOI *syntax*. It does not check whether a DOI is registered with any registry or resolves to anything. For most workflows that's fine — a never-registered DOI simply produces zero citation rows from every adapter, and the result is recorded in `queried_dois` as a successful empty query. But two use cases benefit from an explicit validation step:

- **Auditing an inherited input list** before launching a long batch run, so missing-from-registry DOIs are flagged up front.
- **Distinguishing "uncited dataset" from "DOI doesn't exist"** in downstream reporting against an EDI-scale store.

To support these without forcing the cost on every run, the library exposes a separate function `validate_dois()` (§6.5). Validation is decoupled from citation fetching: the citation entry points never call it, and validation results are persisted alongside (not inside) the per-source citation tables.

**What is checked.** Validation hits the DOI handle resolver directly:

```
HEAD https://doi.org/<doi>
```

A `30x` redirect means the handle resolves (DOI is registered). A `404` means it isn't. Other status codes are recorded as `unknown`. Optionally, validation can additionally probe DataCite (`https://api.datacite.org/dois/<doi>`) and CrossRef (`https://api.crossref.org/works/<doi>`) to identify which registrar holds the DOI and capture minimal metadata (title, resource type) — useful for catching inputs that are CrossRef article DOIs accidentally mixed into a dataset list.

**No new dependencies.** The validation path reuses the existing `httpx` client and `request_with_retry` plumbing. No `crossref-commons` / `datacite-py` / similar wrapper libraries are introduced; they would add little over the REST endpoints we already call.

**Persistence.** When `db_path` is supplied, results are written into `cited_dois` (one row per DOI, with `validation_status` and `validated_at`) and a per-event audit row in `doi_validations` (§7.2). When `db_path` is omitted, the function returns a DataFrame and is not persistent.

**Freshness.** `validate_dois()` honors the same `refresh_after` semantics as the citation entry points: pairs with `validated_at` newer than the cutoff are skipped unless `force=True`.

## 6. Public API

### 6.1 Entry Points

```python
from dataset_citation_finder import (
    find_citations,             # single DOI; in-memory by default, db optional
    find_citations_batch,       # iterable of DOIs; in-memory by default, db optional
    find_citations_by_prefix,   # all DOIs under a prefix; writes to SQLite (db_path required)
    validate_dois,              # check DOI registration / resolvability (separate step)
    collapse_citations,         # rebuild the `citations` table from raw source tables
    export_citations,           # export from SQLite to CSV/Parquet/BibTeX/JSON
    read_citations,             # convenience: load a table or view as a DataFrame
    collapse_citations_df,      # in-memory long DataFrame -> deduplicated DataFrame
)
```

`find_citations` and `find_citations_batch` return a long-format DataFrame (and optionally write to a SQLite path if `db_path` is provided). `find_citations_by_prefix` **requires** a `db_path` and returns a `RunSummary` describing what was written. `collapse_citations_df` operates on an in-memory DataFrame for the single-DOI and batch use cases; for SQLite-backed results, use `read_citations(db_path, view='collapsed')` instead.

### 6.2 Parameters

| Parameter | Type | Description |
|---|---|---|
| `doi` / `dois` / `prefix` | str / Iterable[str] / str | Input DOI, DOI iterable, or DOI prefix |
| `db_path` | str \| Path \| None | Path to SQLite file. Required for `find_citations_by_prefix`; optional for `find_citations` and `find_citations_batch`. Created on first use; if it already exists (whether from a prior run of this tool or from a user-supplied copy), it is opened, schema-validated, and migrated forward as needed before being updated in place. Opened in WAL mode. |
| `sources` | str \| list | `'all'` or subset (e.g., `['openalex', 'datacite_events']`) |
| `email` | str \| None | Polite-pool email for OpenAlex, CrossRef, DataCite |
| `api_keys` | dict \| None | e.g., `{'semantic_scholar': '...', 'dimensions': '...'}` |
| `concurrency` | int | Per-adapter worker count (default 8) |
| `relation_types` | list[str] | DataCite Events relation-type filter (§4.1 note) |
| `refresh_after` | timedelta \| None | If set, skip `(cited_doi, source)` pairs queried more recently than this. Default: re-query everything (`timedelta(0)`) on every run. |
| `force` | bool | If True, ignore `refresh_after`. Default False. |
| `collapse` | bool | Whether to rebuild the `citations` table after the run completes. Default True. |
| `checkpoint_interval` | int | Rows between transaction commits (default 500). |

`RunSummary` is a small dataclass: `run_id`, `started_at`, `finished_at`, `inputs_total`, `inputs_skipped` (skipped by `refresh_after`), `rows_per_source` (dict), `errors_per_source` (dict).

### 6.3 Helper functions

```python
collapse_citations(db_path) -> int
# Rebuild `citations` from current state of source_* tables. Returns row count.

collapse_citations_df(df) -> pd.DataFrame
# In-memory equivalent: collapse a long-format DataFrame to one row per
# (cited_doi, citing_doi) edge, with `sources` and `relation_types` lists.
# Used by find_citations and find_citations_batch when no db_path is provided.

export_citations(db_path, output_path, *, format='csv', view='collapsed') -> Path
# format: 'csv' | 'parquet' | 'bibtex' | 'json'
# view:   'collapsed' | 'long' | '<source_name>'

read_citations(db_path, *, view='collapsed') -> pd.DataFrame
# Convenience SELECT wrapper.

open_store(db_path, *, create=True, migrate=True) -> StoreHandle
# Open (or create) an existing store. Validates the schema_version row,
# runs any pending migrations, and returns a connection-like handle.
# Raises StoreFormatError if the file exists but is not a recognizable
# dataset-citation-finder database, or SchemaVersionError if the file
# was written by a newer library version than the caller. Used internally
# by every entry point above; exposed publicly for inspection and admin tasks.

describe_store(db_path) -> StoreInfo
# Read-only summary: schema_version, library_version that wrote it,
# created_at, last_run_at, row counts per source table, citations row count,
# distinct cited_dois, list of run_ids. Does not modify the file.
```

### 6.4 Behavior when `db_path` is omitted

`find_citations` and `find_citations_batch` may be called without a `db_path`. In that mode:

- The engine runs the in-memory pipeline (`engine.collect_rows` → list of `LongRow`).
- The return value is a long-format pandas DataFrame (or a list of `GroupedRecord` if `output='grouped'`), not a `RunSummary`.
- **No `cited_dois`, `queried_dois`, `runs`, or per-source rows are persisted** — the run leaves no trace.
- `refresh_after`, `force`, `coalesce`, and `checkpoint_interval` are silently ignored (they have no store to act on).
- Dedup is available only via `collapse_citations_df(df)` on the returned DataFrame.

This mode is intended for ad-hoc exploration, single-DOI lookups, and small batches (≲100 DOIs). For batches above that threshold the library logs an INFO-level recommendation to supply a `db_path`, since the in-memory result has no resumability and re-runs cannot benefit from the freshness filter. `find_citations_by_prefix` does not offer this mode at all — prefix sweeps are too large for the in-memory path to be useful and `db_path` is required.

### 6.5 `validate_dois`

```python
validate_dois(
    dois,                       # iterable of DOIs (single DOI also accepted)
    *,
    db_path=None,               # optional; when provided, results persist
    email=None,                 # polite-pool header
    refresh_after=None,         # skip DOIs validated more recently than this
    force=False,                # ignore refresh_after
    check_registrar=True,       # also probe DataCite + CrossRef metadata APIs
    concurrency=8,
) -> pd.DataFrame | RunSummary
```

Validates each input DOI by issuing `HEAD https://doi.org/<doi>`. When `check_registrar=True`, follows up with a metadata request to DataCite or CrossRef (whichever responds 200 first) to identify the registrar and capture minimal metadata (title, resource type).

**Return shape.**

- Without `db_path`: returns a DataFrame with columns `cited_doi`, `validation_status`, `validated_at`, `validated_registrar`, `notes`.
- With `db_path`: writes/updates rows in `cited_dois` (and appends per-event audit rows in `doi_validations`) and returns a `RunSummary` with `input_kind='validate'`.

**Status values.**

- `'registered'` — handle resolver returned a redirect.
- `'unresolved'` — handle resolver returned 404.
- `'unknown'` — non-2xx/3xx/4xx response (e.g., 503), or all retries exhausted.
- `'unchecked'` — placeholder used by other code paths when a DOI is added to `cited_dois` from input/enumeration but validation has not run.

**Recommended workflow.**

```python
# Audit then fetch.
val = validate_dois(dois, db_path="store.sqlite", email="me@example.org")
good = read_citations("store.sqlite", view="cited_dois")
good = good[good["validation_status"] == "registered"]["cited_doi"].tolist()
find_citations_batch(good, db_path="store.sqlite", email="me@example.org")
```

The citation entry points themselves never call `validate_dois`. They simply pass every input DOI to every adapter and rely on adapters to return zero rows for non-existent DOIs. This keeps the cost of validation opt-in and the citation pipeline's behavior unchanged for users who don't need it.

### 6.6 Working with an existing user-supplied database

Any entry point that accepts `db_path` operates idempotently on an existing file:

- **Identification.** On open, the library checks for a `meta` table (§7.2) and validates that `meta.schema_version` is present and `<=` the library's current schema version. If the file exists but lacks the `meta` table or has a higher version than the library understands, the library raises `StoreFormatError` or `SchemaVersionError` rather than silently corrupting the file. The user can downgrade the library or upgrade and retry.
- **Migration.** If `meta.schema_version` is older than the current version, the library runs ordered migrations (§7.4) inside a single transaction before accepting writes. Migrations are idempotent — re-running has no effect.
- **Update semantics.** Subsequent writes go through the same upsert path (§7.3): existing rows have their `last_retrieved_at` advanced and `run_id` updated; `first_retrieved_at` is preserved. New rows are inserted. The `runs` table accumulates one new row per invocation, so the file carries its own audit trail across owners and machines.
- **Read-only inspection.** `describe_store(db_path)` and `read_citations(db_path, …)` open the file in read-only mode and never write, so they are safe to call against a database currently being updated by another process (WAL mode permits concurrent readers).
- **Portability.** The store is a single SQLite file with no external dependencies; users can copy, share, archive, or check it into version control. Re-opening a copy on a different machine works without configuration. Source-specific upstream IDs are preserved so that a recipient can re-derive provenance even if they did not perform the original queries.

## 7. Internal Schema

### 7.1 In-memory pydantic models

`LongRow` and `GroupedRecord` (in `schema.py`) remain the in-memory transit format between adapters and the store, and the return type for `find_citations` (in long-format DataFrame form, via `pd.DataFrame.from_records`).

**Grouped citation record** (returned by `read_citations(..., view='collapsed')` when materialized to objects):

| Field | Type | Notes |
|---|---|---|
| `doi` | str \| None | Canonical DOI of the citing work (normalized) |
| `title` | str | Paper title |
| `year` | int \| None | Publication year |
| `authors` | list[str] | Formatted as `"Last, First"` |
| `venue` | str \| None | Journal or conference name |
| `abstract` | str \| None | If provided by source |
| `sources` | list[str] | Adapters that returned this record |
| `openalex_id` | str \| None | OpenAlex work ID |
| `semantic_scholar_id` | str \| None | Semantic Scholar corpus ID |
| `datacite_event_ids` | list[str] \| None | Supporting DataCite event UUIDs |
| `datacite_relation_types` | list[str] \| None | Distinct DataCite relation types observed |
| `crossref_score` | float \| None | CrossRef relevance score |
| `url` | str \| None | Best available URL |

**Long-format row** (one per source observation; the unit emitted by adapters):

| Column | Type | Description |
|---|---|---|
| `cited_doi` | str | Normalized DOI of the dataset |
| `citing_doi` | str | Normalized DOI of the citing work |
| `source` | str | Adapter identifier |
| `relation_type` | str \| None | Source-reported relation type (e.g., `is-cited-by`, `references`). Populated by DataCite Events; `None` for sources that don't distinguish. |
| `retrieved_at` | datetime (ISO 8601, UTC) | When fetched |

A single `(cited_doi, citing_doi, source)` edge may appear as multiple long rows if a source reports more than one relation type for it.

### 7.2 SQLite store schema

Every per-source table shares a common core (`cited_doi`, `citing_doi`, `relation_type`, `first_retrieved_at`, `last_retrieved_at`, `run_id`) plus source-specific columns. All timestamps are ISO 8601 UTC strings. JSON-typed columns are stored as TEXT and parsed by callers.

```sql
-- Store metadata. Exactly one row. Read first on every open() to validate
-- that this file is a dataset-citation-finder database and to drive migrations.
CREATE TABLE meta (
    schema_version   INTEGER NOT NULL,           -- bumped by each migration
    library_version  TEXT NOT NULL,              -- last library version that touched the file
    created_at       TEXT NOT NULL,              -- when the store was first created
    created_by       TEXT,                       -- library version that created it
    application_id   INTEGER NOT NULL DEFAULT 0x44434646  -- 'DCFF', also set as PRAGMA application_id
);

-- Run log: one row per find_citations_batch / find_citations_by_prefix invocation.
CREATE TABLE runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    input_kind        TEXT NOT NULL,    -- 'single' | 'batch' | 'prefix' | 'validate'
    input_value       TEXT,             -- prefix string, or first-N hash for batches
    input_count       INTEGER,
    sources_requested TEXT NOT NULL,    -- JSON array
    sources_succeeded TEXT,             -- JSON array
    rows_written      INTEGER,
    notes             TEXT
);

-- The "in-scope" DOI set: every DOI ever submitted as a query input, plus
-- every DOI surfaced by prefix enumeration. This is the answer to "what
-- DOIs are we tracking in this store?" — independent of whether any source
-- has actually returned citation data for them.
--
-- A DOI lives here whether or not it is cited. Many will not be — that's the
-- point: `LEFT JOIN cited_dois LEFT JOIN citations` distinguishes "uncited
-- dataset" from "DOI we have not yet queried".
--
-- This is also where DOI registry validation results live (one column per
-- DOI). Per-event validation history is appended to `doi_validations` below.
CREATE TABLE cited_dois (
    cited_doi           TEXT PRIMARY KEY,
    first_seen_at       TEXT NOT NULL,
    first_seen_run_id   INTEGER REFERENCES runs(run_id),
    last_seen_at        TEXT NOT NULL,
    last_seen_run_id    INTEGER REFERENCES runs(run_id),
    origin              TEXT NOT NULL,    -- 'input' | 'prefix:<prefix>' | 'validation'
    validation_status   TEXT,             -- 'unchecked' | 'registered' | 'unresolved' | 'unknown'
    validated_at        TEXT,
    validated_registrar TEXT              -- 'datacite' | 'crossref' | NULL
);

-- Per-DOI/per-source citation-fetch freshness, used by `refresh_after` to
-- decide whether to re-query an adapter for a DOI. Distinct from `cited_dois`
-- (which records that the DOI is in scope at all): `queried_dois` records
-- that adapter X was actually dispatched against DOI Y at time T. Absence of
-- a row means "this source has never been asked about this DOI".
CREATE TABLE queried_dois (
    cited_doi        TEXT NOT NULL,
    source           TEXT NOT NULL,
    last_queried_at  TEXT NOT NULL,
    last_run_id      INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, source),
    FOREIGN KEY (cited_doi) REFERENCES cited_dois(cited_doi)
);

-- Append-only audit log of individual validation events. `cited_dois` carries
-- only the latest status; this table preserves the full history (e.g., if a
-- DOI was unresolvable at one time and registered later).
CREATE TABLE doi_validations (
    cited_doi    TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    method       TEXT NOT NULL,    -- 'doi.org' | 'datacite' | 'crossref'
    status       TEXT NOT NULL,    -- 'registered' | 'unresolved' | 'unknown'
    http_status  INTEGER,
    registrar    TEXT,
    notes        TEXT,
    run_id       INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, validated_at, method),
    FOREIGN KEY (cited_doi) REFERENCES cited_dois(cited_doi)
);

-- DataCite Events: events have stable IDs, so PK is event_id.
CREATE TABLE source_datacite_events (
    event_id            TEXT PRIMARY KEY,
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    relation_type       TEXT NOT NULL,
    upstream_source     TEXT,           -- e.g., 'crossref', 'datacite'
    occurred_at         TEXT,
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id)
);
CREATE INDEX idx_dce_pair ON source_datacite_events (cited_doi, citing_doi);

-- OpenAlex: per-pair, one row per edge.
CREATE TABLE source_openalex (
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    citing_openalex_id  TEXT,
    cited_openalex_id   TEXT,
    relation_type       TEXT,           -- nullable; OpenAlex doesn't distinguish
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, citing_doi)
);

-- OpenCitations.
CREATE TABLE source_opencitations (
    cited_doi           TEXT NOT NULL,
    citing_doi          TEXT NOT NULL,
    creation_date       TEXT,
    journal_sc          TEXT,           -- 'yes' | 'no' | NULL
    author_sc           TEXT,
    relation_type       TEXT,
    first_retrieved_at  TEXT NOT NULL,
    last_retrieved_at   TEXT NOT NULL,
    run_id              INTEGER REFERENCES runs(run_id),
    PRIMARY KEY (cited_doi, citing_doi)
);

-- Stub adapters will follow the same pattern when implemented:
--   source_semantic_scholar, source_crossref, source_dimensions, source_google_scholar.

-- Collapsed canonical edges. Rebuilt by collapse_citations().
CREATE TABLE citations (
    cited_doi          TEXT NOT NULL,
    citing_doi         TEXT NOT NULL,
    sources            TEXT NOT NULL,   -- JSON array of source ids
    source_count       INTEGER NOT NULL,
    relation_types     TEXT,            -- JSON array; may be empty
    first_retrieved_at TEXT NOT NULL,
    last_retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (cited_doi, citing_doi)
);
CREATE INDEX idx_citations_citing ON citations (citing_doi);
```

The store is opened in **WAL mode** with `PRAGMA foreign_keys=ON`. Adapter writes are batched and committed in transactions sized to `checkpoint_interval` (default 500 rows).

### 7.3 Upsert semantics

- **`cited_dois`**: `INSERT ... ON CONFLICT(cited_doi) DO UPDATE SET last_seen_at = excluded.last_seen_at, last_seen_run_id = excluded.last_seen_run_id`. `first_seen_at`, `first_seen_run_id`, and `origin` are preserved on conflict. `validation_status` / `validated_at` / `validated_registrar` are touched only by `validate_dois()`.
- **DataCite Events**: `INSERT ... ON CONFLICT(event_id) DO UPDATE SET last_retrieved_at = excluded.last_retrieved_at, run_id = excluded.run_id`.
- **All other per-source tables**: `INSERT ... ON CONFLICT(cited_doi, citing_doi) DO UPDATE SET last_retrieved_at = excluded.last_retrieved_at, run_id = excluded.run_id` (and any newly observed source-specific columns merged via SQL `COALESCE()`; this is the SQL function and is unrelated to the library's own `collapse` step).
- **`first_retrieved_at`** is preserved on conflict; only `last_retrieved_at` advances.
- **`doi_validations`**: append-only; one row per `(cited_doi, validated_at, method)`. Never updated.
- **`collapse_citations()`** truncates and rebuilds `citations` in a single transaction. Future optimization: incremental collapse on dirty pairs only (tracked via `run_id`).

**Population order within a run.** Every entry point that writes to the store first upserts every input/enumerated DOI into `cited_dois` (so the in-scope set is recorded even if no adapter returns anything), then dispatches adapters which populate the `source_*` tables and bump `queried_dois`, then optionally rebuilds `citations` via `collapse_citations()`. `validate_dois()` only touches `cited_dois` and `doi_validations`.

### 7.4 Open, validate, migrate

The single entry point into the store is `open_store(db_path, *, create=True, migrate=True)`. Its contract:

1. If the file does not exist and `create=True`, create it, set `PRAGMA application_id = 0x44434646` and `PRAGMA user_version = <current_schema_version>`, run all DDL above, insert the single `meta` row, and return.
2. If the file exists, open it and verify `PRAGMA application_id`. If mismatched (and non-zero), raise `StoreFormatError` — the file was not created by this library.
3. Read `meta.schema_version`. If absent, raise `StoreFormatError`. If greater than the library's current version, raise `SchemaVersionError` — the file was written by a newer library version. If equal, proceed. If less, run pending migrations in order (each as its own transaction) when `migrate=True`; otherwise raise `MigrationRequired`.
4. Update `meta.library_version` to the current library version on every successful open.
5. Set `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on every open.

Migrations live in `store/migrations/` as numbered Python modules (`001_initial.py`, `002_add_X.py`, …). Each declares `from_version`, `to_version`, and an `apply(conn)` function. New tables for new adapters are added via migrations, never by ad-hoc DDL — so a user-supplied database produced by an older version automatically grows the new tables on first open by a newer version.

Migrations are forward-only by design. The library version that wrote a file is recorded in `meta.created_by` and `meta.library_version` so that a user opening an unfamiliar file can see what produced it.

## 8. Adapter Details

### 8.1 DataCite Event Data

- **Endpoint**: `https://api.datacite.org/events`
- **Pagination**: cursor-based (`page[cursor]`)
- **Key queries**:
  - Per-DOI: `obj-id=<doi>&relation-type-id=<type>` (one request per relation type, or omit the filter and filter client-side)
  - Prefix: `obj-id.prefix=<prefix>&relation-type-id=<type>`
- **Default relation types counted as citations**: all six citation/reference types — `is-cited-by`, `is-referenced-by`, `is-supplement-to`, `cites`, `references`, `is-supplemented-by`. The first three are populated when a data repository records a citation from the dataset's side; the latter three are populated when a paper's reference list contains the dataset DOI — notably, CrossRef-sourced events arrive as `references`, so excluding them would miss the bulk of real citations.
- **Excluded by default** (structural, not citations): `is-part-of`, `is-version-of`, `has-version`, `is-new-version-of`, `is-previous-version-of`, `is-derived-from`, `is-source-of`, `is-continued-by`, `continues`, `is-identical-to`, `has-part`, `is-variant-form-of`, `is-original-form-of`.
- **Relation-type normalization**: the adapter treats the three inverse pairs symmetrically. When emitting a record, `cited_doi` is always the dataset and `citing_doi` is always the citing work, regardless of which direction the event was recorded in. The original raw relation type is preserved in the `relation_type` column.
- **Output mapping**: `cited_doi`/`citing_doi` derived from `obj-id`/`subj-id` according to relation direction. `source_id = "datacite_events"`. Additional metadata includes upstream `source-id` (e.g., `crossref`, `datacite`) and event timestamp; both persisted to `source_datacite_events`.

### 8.2 OpenCitations

- **Endpoint**: `https://opencitations.net/index/api/v2/citations/{doi}`
- **Pagination**: single-call, full result set
- **Batch strategy**: per-DOI via concurrent worker pool (default 8).
- **Output mapping**: `cited_doi` = input DOI; `citing_doi` = `citing` field. Metadata: `creation` date, `journal_sc` / `author_sc` self-citation flags persisted to `source_opencitations`.

### 8.3 Dimensions (optional)

- **Endpoint**: `https://app.dimensions.ai/api/dsl.json`
- **Auth**: token (from `/api/auth` with username/password) or API key. Adapter registers only if credentials detected via config dict or `DIMENSIONS_USER` / `DIMENSIONS_PASS` / `DIMENSIONS_API_KEY` env vars. Absence → one INFO log line; no warnings.
- **Query** (native batch):

  ```
  search publications where dataset_dois in ["10.xxxx/...", "10.yyyy/...", ...]
  return publications[id+doi+title+year+authors]
  limit 1000
  ```

- Initial batch size: 100 DOIs; self-tunes based on response size and rate-limit headers.

### 8.4 OpenAlex, Semantic Scholar, CrossRef

Existing adapters from the original design; unchanged. Each implements concurrent per-DOI queries with polite-pool headers and exponential backoff.

### 8.5 Google Scholar (opt-in)

Retired from default. Uses `scholarly` library with configurable delay and optional proxy support. Requires explicit inclusion in `sources=[...]` to activate.

## 9. Scale, Batching, Resumability

Three auto-selected paths:

**a. Prefix path** — `find_citations_by_prefix(prefix, db_path)` uses DataCite Events' native prefix filter for that adapter; other adapters receive an enumerated DOI list from `PrefixEnumerator`, filtered by `queried_dois` if `refresh_after` is set.

**b. Batch path** — `find_citations_batch(dois, db_path)` dispatches each adapter to its most efficient mode: native batching (Dimensions), concurrent per-DOI (OpenAlex, Semantic Scholar, CrossRef, OpenCitations), or prefix-aggregated-then-filtered when the input list shares a common prefix and the adapter supports prefix queries (DataCite Events). The DOI list is filtered against `queried_dois` per source before dispatch.

**c. Single path** — `find_citations(doi)` queries each adapter per-DOI; returns in-memory DataFrame. If `db_path` is provided, also writes to the store as `input_kind='single'`.

**Cross-cutting concerns:**

- **Concurrency** is bounded per-adapter (default 8 workers each).
- **Rate limiting** uses per-adapter token buckets; 429 responses trigger exponential backoff with jitter.
- **Resumability is now a property of the store, not a separate checkpoint file.** Each row written carries a `run_id`; each `(cited_doi, source)` queried bumps `queried_dois`. An interrupted run can be resumed by re-invoking with the same arguments and a non-zero `refresh_after`, which causes the freshness filter to skip everything queried in the recent window.
- **Periodic updates** are the same call with the same arguments and a non-zero `refresh_after` (e.g., `timedelta(days=30)`) to re-query stale pairs.
- **Progress** is surfaced via an optional `tqdm` bar.

## 10. Error Handling

- Each adapter implements exponential backoff with jitter on 429/503 responses.
- Per-source failures are non-fatal: errors are recorded in `runs.notes` and reflected in `RunSummary.errors_per_source`, but never abort the run.
- `RateLimitError` is raised (not swallowed) only if all retries are exhausted *and* the adapter is configured to fail-fast (default: fail-soft, log and continue).
- Google Scholar adapter includes configurable delay and optional proxy support.

## 11. Configuration

```python
config = {
    "db_path": "~/data/edi_citations.sqlite",
    "sources": [
        "datacite_events",
        "openalex",
        "semantic_scholar",
        "crossref",
        "opencitations",
        # "dimensions"  -- auto-added if credentials present
        # "google_scholar"  -- opt-in only
    ],
    "polite_email": "gregory@example.org",
    "cache_dir": "~/.cache/dataset_citation_finder",
    "concurrency": 8,
    "relation_types": [
        "is-cited-by", "is-referenced-by", "is-supplement-to",
        "cites", "references", "is-supplemented-by",
    ],
    "refresh_after": timedelta(days=30),
    "checkpoint_interval": 500,
    "api_keys": {
        "semantic_scholar": "...",  # optional
    },
    "dimensions": {  # optional
        "username": "...",
        "password": "...",
    },
}
```

## 12. Dependencies

| Package | Purpose |
|---|---|
| `httpx` | Async HTTP client for API adapters |
| `rapidfuzz` | Fuzzy title matching for dedup fallback (deferred) |
| `bibtexparser` | BibTeX export |
| `pandas` | DataFrame return / CSV export |
| `pyarrow` | Parquet export (optional) |
| `pydantic` | In-memory record schema validation |
| `tqdm` | Progress bars (optional) |
| `scholarly` | Google Scholar scraping (opt-in) |
| `sqlite3` (stdlib) | Canonical store |

## 13. Resolved Design Decisions

Previously open questions from the original design, now decided:

- **Prefix expansion strategy** → Two-path: DataCite Events uses its native prefix filter; other adapters use a shared `PrefixEnumerator` backed by DataCite's `/dois` endpoint with disk caching.
- **Google Scholar status** → Retired from default source set; remains as opt-in.
- **Multi-source output** → Persisted to a SQLite store with one raw table per adapter and a collapsed `citations` table aggregating sources. The in-memory `collapse_citations_df(df)` helper is used for the single-DOI and batch paths if no SQLite store is being used.
- **Relation-type scope** → Include all six DataCite citation/reference relation types by default: `is-cited-by`, `is-referenced-by`, `is-supplement-to`, `cites`, `references`, `is-supplemented-by`. Rationale: the three "citation" types (dataset-side) and the three "reference" types (paper-side) describe the same underlying relationship from opposite directions. Critically, CrossRef-derived events in DataCite Event Data use `references`, so filtering to only `is-cited-by` would exclude the majority of real-world citations. Raw relation type is preserved per-record so downstream consumers can re-filter.
- **Async exposure** → Public API is sync-only. Adapters may use async HTTP internally (via `httpx.AsyncClient`) for concurrency within a run, but the public entry points are ordinary blocking functions. Rationale: keeps the library easy to use from scripts, notebooks, and non-async codebases; async concurrency is an implementation detail, not a user concern.
- **Output and checkpoint store format** → **SQLite is now the canonical store for batch and prefix runs.** Per-source raw tables preserve every observation; the `citations` table is a collapsed view rebuilt on demand. This unifies what the original design treated as two separate concerns (a SQLite checkpoint store + final-export Parquet/CSV/JSON files). CSV, Parquet, BibTeX, and JSON remain available as exports derived from the store (or in-memory operations); they are no longer the canonical output.
- **DOI registry validation** → Previously a non-goal; now an optional, separate step exposed as `validate_dois(...)`. It is intentionally decoupled from the citation-fetch pipeline so users can audit a list, persist the result, and feed only resolvable DOIs back into `find_citations_batch` / `find_citations_by_prefix` — but the citation entry points themselves never validate, so the cost is opt-in. Detailed contract specified in §6.5 / §7.2.

## 14. Open Questions

1. **Versioned DOIs.** Many repositories (including EDI) issue per-version DOIs. A citation to v1 vs v2 is semantically distinct but often reflects the same underlying usage. Should a "concept DOI" rollup be provided as an optional post-processing step? Deferred — unclear whether versioned DOIs can be reliably distinguished across repositories; revisit after initial data is in hand.
2. **Dimensions batch size and rate limits.** Needs empirical testing once credentials exist.
3. **CrossRef adapter retention.** OpenCitations and DataCite Events both harvest CrossRef, so the standalone CrossRef adapter may be redundant. Test on a fixture of known-cited dataset DOIs: count edges found only by the CrossRef adapter (not propagated to OpenCitations or DataCite Events). If that count is negligible, demote CrossRef to opt-in.
4. **Semantic Scholar coverage of datasets.** Worth measuring on a fixture of known-cited dataset DOIs across repositories; may be weak enough to demote to opt-in.
5. **Companion metadata table.** The current schema captures only edges (cited_doi, citing_doi). Adapters that return rich metadata (title, authors, year, venue) drop most of it. A `citing_works` table keyed on `citing_doi` is a natural extension; deferred until at least one consumer needs it.

## 15. Implementation Plan

The base SQLite refactor (steps 1–9 below) is **complete** as of v0.2.0. The remaining work is the validation step, the `cited_dois` schema split, and the related entry-point updates from this revision (steps 10–14).

**Completed (v0.2.0):**

1. **`store/` module**: schema DDL, connection helpers (WAL mode, foreign keys), `open_store()` with `application_id` / `meta.schema_version` validation, and a `migrations/` subpackage of numbered, idempotent migration scripts.
2. **`store/upsert.py`**: per-source upsert helpers; one function per source table with the right ON CONFLICT clause.
3. **`engine.py` refactor**: route adapter output into the store via `collect_to_store`. Single-DOI in-memory path retained as `collect_rows`.
4. **`collapse.py` / `store/coalesce.py`**: SQL-based `collapse_citations(db_path)` that rebuilds the `citations` table; in-memory `collapse_citations_df(df)` retained.
5. **`api.py` refactor**: `find_citations`, `find_citations_batch`, `find_citations_by_prefix` with the entry-point semantics described in §6.1.
6. **Public helpers**: `collapse_citations`, `export_citations`, `read_citations`, `open_store`, `describe_store`.
7. **`queried_dois` integration**: pre-filter input DOIs by freshness window; bump after each successful query.
8. **Open-existing-store handling**: `application_id` validation, schema-version checks, forward-only migrations.
9. **Test suite (118 tests)**: schema, upsert, collapse, refresh, open-existing, end-to-end batch + prefix.

**To do (this revision):**

10. **`cited_dois` and `doi_validations` migration** (`m002_cited_dois.py`):
    - Add the two tables defined in §7.2.
    - Bump `CURRENT_SCHEMA_VERSION` to 2.
    - Add `cited_dois` upsert helper to `store/upsert.py`.
    - Existing v1 stores auto-upgrade on next `open_store()` call.
11. **`api.py` updates for `cited_dois` population**:
    - At run start, every entry point with `db_path` upserts each input/enumerated DOI into `cited_dois` with the appropriate `origin`.
    - Update `find_citations_batch` to accept `db_path=None` (in-memory mode); document in §6.4.
    - Add the recommendation-log when batch input exceeds ~100 DOIs without `db_path`.
12. **`validate_dois` implementation** (new `validation.py`):
    - HEAD `https://doi.org/<doi>` via existing `request_with_retry`.
    - Optional registrar probes against DataCite and CrossRef metadata endpoints (reuse existing client; no new dependency).
    - Two write modes: in-memory DataFrame, or persist to `cited_dois` + `doi_validations`.
    - Honor `refresh_after` / `force` for re-validation freshness.
    - Add to public API surface in `__init__.py` and `api.py`.
13. **`read_citations` view extension**: add `view='cited_dois'` and `view='doi_validations'` to the §6.3 helper.
14. **Tests**:
    - Migration v1 → v2 against a v0.2.0 fixture file.
    - `cited_dois` upsert: `first_seen_at` preserved, `last_seen_at` advanced.
    - `validate_dois`: registered/unresolved/unknown status mapping; `refresh_after` skipping; persistence vs. in-memory return; `RunSummary` with `input_kind='validate'`.
    - End-to-end audit-then-fetch workflow.
    - Update README and CLAUDE.md.

## 16. Future Work

- CLI interface wrapping the primary functions.
- R package (e.g., `rcitations`) calling the Python library via `reticulate`.
- Full-text mention detection for informal dataset citations.
- Scopus and Web of Science adapters (when institutional access is available).
- Incremental collapse on dirty pairs only (tracked via `run_id`), avoiding full table rebuild on each run.
- Multi-database merge utility (combine two SQLite stores from separate institutions).
- Companion `citing_works` metadata table populated by adapters that return it.

## 17. Deliverables

- Python library with adapters, merge engine, SQLite store, and batch/prefix entry points.
- SQLite-backed resumability and periodic-refresh support.
- Example notebook demonstrating (a) a single-DOI lookup, (b) a batch run over a custom list with a SQLite store, (c) a full-repository sweep via prefix, and (d) a periodic refresh against an existing store.
- This design document (committed alongside the library).
