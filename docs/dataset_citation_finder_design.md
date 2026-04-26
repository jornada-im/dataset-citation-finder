# Dataset Citation Finder — Design & Planning Document

*Single consolidated design document. Supersedes the earlier `.docx` design.*

## 1. Overview

The **Dataset Citation Finder** is a Python library for discovering academic papers that cite a given dataset. It accepts a full DOI, a list of DOIs, or a DOI prefix (e.g., `10.6073/*` to sweep an entire dataset family such as the Environmental Data Initiative's catalog); queries multiple scholarly and citation-graph services; and returns deduplicated results with per-source provenance preserved.

The tool is designed as a Python library first — not a CLI or web service — and is repository-agnostic: dataset DOIs from any DataCite- or CrossRef-registered repository are valid input.

## 2. Goals

- Accept input as (a) a single dataset DOI, (b) an arbitrary iterable of DOIs, or (c) a DOI prefix.
- Query multiple citation sources (see §4) and merge results.
- Preserve per-source provenance on every record — provenance is a first-class concern, not an afterthought.
- Output structured data in JSON, CSV, BibTeX, and a long-format provenance-preserving table.
- Run efficiently at scale: a single-DOI lookup and a full-repository sweep (tens of thousands of DOIs) are the same pipeline, with automatic path selection and resumable runs.

## 3. Non-Goals

- Does not resolve or validate DOIs against a registry (beyond basic syntactic checks).
- Does not crawl full-text of papers to find informal dataset mentions.
- No R package or CLI interface in v1 (possible future work).

## 4. Architecture

Three layers: **source adapters → merge/dedup engine → output formatters.**

Each adapter implements a common interface: given a DOI (or batch of DOIs), return a sequence of `CitationRecord` objects with at minimum `cited_doi`, `citing_doi`, and `source_id`, plus optional metadata. The engine collects records from all configured adapters and dispatches them to the selected formatter.

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

### 4.2 Merge / Dedup Engine

Records from multiple sources are merged on normalized DOI (§5). When a citing work lacks a DOI, secondary matching uses fuzzy title + year + first-author. The engine supports two output shapes:

1. **Grouped records**: one record per unique citing work, with per-source provenance attached as a sub-structure (the `sources` list and `*_id` fields).
2. **Long-format table**: one row per `(cited_doi, citing_doi, source)` tuple — intentionally undeduplicated, for auditability.

A helper `collapse_sources(df)` converts long-format to a deduplicated view with `sources` as a list.

### 4.3 Output Formatters

- **JSON**: list of merged citation records (grouped shape).
- **CSV**: flat tabular export; multi-valued fields pipe-delimited.
- **BibTeX**: one `@article` entry per record; non-standard fields (`sources`, upstream IDs) emitted as `note` fields.
- **Long-format table**: pandas DataFrame, writable to CSV or Parquet. Parquet preferred for large runs.

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

Results are cached on disk (default TTL 24 h) since repository catalogs change slowly.

## 6. Public API

### 6.1 Entry Points

```python
from dataset_citation_finder import (
    find_citations,             # single DOI
    find_citations_batch,       # iterable of DOIs
    find_citations_by_prefix, # all DOIs under a DOI prefix
    collapse_sources,           # long-format -> deduplicated
)
```

All three entry points accept the same keyword arguments and return the same record structure. The choice among them is about *how input DOIs are supplied*, not what the tool does with them.

```python
find_citations(doi, sources='all', output='json', email=None, api_keys=None, ...)
find_citations_batch(dois, sources='all', output='long', output_path=None, ...)
find_citations_by_prefix(prefix, sources='all', output='long', ...)
```

### 6.2 Parameters

| Parameter | Type | Description |
|---|---|---|
| `doi` / `dois` / `prefix` | str / Iterable[str] / str | Input DOI, DOI iterable, or DOI prefix |
| `sources` | str \| list | `'all'` or subset (e.g., `['openalex', 'datacite_events']`) |
| `output` | str | `'json'`, `'csv'`, `'bibtex'`, or `'long'` |
| `output_path` | str \| Path \| None | Destination for CSV/Parquet/BibTeX outputs |
| `email` | str \| None | Polite-pool email for OpenAlex, CrossRef, DataCite |
| `api_keys` | dict \| None | e.g., `{'semantic_scholar': '...', 'dimensions': '...'}` |
| `concurrency` | int | Per-adapter worker count (default 8) |
| `relation_types` | list[str] | DataCite Events relation-type filter (§4.1 note) |
| `checkpoint_interval` | int | Records between checkpoint writes (default 500) |

## 7. Internal Record Schema

**Grouped citation record** (existing shape):

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
| `datacite_relation_types` | list[str] \| None | Distinct DataCite relation types observed for this edge (e.g., `["is-cited-by", "references"]`) |
| `crossref_score` | float \| None | CrossRef relevance score |
| `url` | str \| None | Best available URL |

**Long-format row**:

| Column | Type | Description |
|---|---|---|
| `cited_doi` | str | Normalized DOI of the dataset |
| `citing_doi` | str | Normalized DOI of the citing work |
| `source` | str | Adapter identifier |
| `relation_type` | str \| None | Relation type reported by the source (e.g., `is-cited-by`, `references`). Populated by DataCite Events; `None` for sources that don't distinguish (OpenAlex, Semantic Scholar, CrossRef, OpenCitations, Dimensions) |
| `retrieved_at` | datetime (ISO 8601, UTC) | When fetched |
| `citing_metadata_id` | str \| None | Key into companion metadata table |

A single `(cited_doi, citing_doi, source)` edge may appear as multiple rows if a source reports more than one relation type for it — e.g., DataCite Events may have both an `is-cited-by` event and a `references` event for the same pair. This is intentional: each event is its own row. `collapse_sources()` groups on `(cited_doi, citing_doi)` and produces a `relation_types` list alongside the `sources` list.

The `collapse_sources()` helper adds `sources` (list), `source_count` (int), `relation_types` (list), and `first_retrieved_at` (datetime) to the deduplicated view.

## 8. Adapter Details

### 8.1 DataCite Event Data

- **Endpoint**: `https://api.datacite.org/events`
- **Pagination**: cursor-based (`page[cursor]`)
- **Key queries**:
  - Per-DOI: `obj-id=<doi>&relation-type-id=<type>` (one request per relation type, or omit the filter and filter client-side)
  - Prefix: `obj-id.prefix=<prefix>&relation-type-id=<type>`
- **Default relation types counted as citations**: all six citation/reference types — `is-cited-by`, `is-referenced-by`, `is-supplement-to`, `cites`, `references`, `is-supplemented-by`. The first three are populated when a data repository records a citation from the dataset's side; the latter three are populated when a paper's reference list contains the dataset DOI — notably, CrossRef-sourced events arrive as `references`, so excluding them would miss the bulk of real citations.
- **Excluded by default** (structural, not citations): `is-part-of`, `is-version-of`, `has-version`, `is-new-version-of`, `is-previous-version-of`, `is-derived-from`, `is-source-of`, `is-continued-by`, `continues`, `is-identical-to`, `has-part`, `is-variant-form-of`, `is-original-form-of`.
- **Relation-type normalization**: the adapter treats the three inverse pairs symmetrically. When emitting a record, `cited_doi` is always the dataset and `citing_doi` is always the citing work, regardless of which direction the event was recorded in. The original raw relation type is preserved in the output's `relation_type` column (§7).
- **Output mapping**: `cited_doi`/`citing_doi` derived from `obj-id`/`subj-id` according to relation direction. `source_id = "datacite_events"`. `relation_type` = raw event relation type. Additional metadata includes upstream `source-id` (e.g., `crossref`, `datacite`) and event timestamp.

### 8.2 OpenCitations

- **Endpoint**: `https://opencitations.net/index/api/v2/citations/{doi}`
- **Pagination**: single-call, full result set
- **Batch strategy**: per-DOI via concurrent worker pool (default 8).
- **Output mapping**: `cited_doi` = input DOI; `citing_doi` = `citing` field. Metadata: `creation` date, `journal_sc` / `author_sc` self-citation flags.

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

**a. Prefix path** — `find_citations_by_prefix(prefix)` uses DataCite Events' native prefix filter for that adapter; other adapters receive an enumerated DOI list from `PrefixEnumerator`.

**b. Batch path** — `find_citations_batch(dois)` dispatches each adapter to its most efficient mode: native batching (Dimensions), concurrent per-DOI (OpenAlex, Semantic Scholar, CrossRef, OpenCitations), or prefix-aggregated-then-filtered when the input list shares a common prefix and the adapter supports prefix queries (DataCite Events).

**c. Single path** — `find_citations(doi)` queries each adapter per-DOI.

**Cross-cutting concerns:**

- **Concurrency** is bounded per-adapter (default 8 workers each) so no single source saturates the network or trips a rate limit.
- **Rate limiting** uses a per-adapter token bucket with defaults tuned to published limits; 429 responses trigger exponential backoff with jitter.
- **Resumability**: long runs write to an append-mostly SQLite checkpoint store keyed on `(source, cited_doi)`; re-invoking with the same output path queries only unprocessed pairs.
- **Checkpoint interval** is configurable (default: every 500 records).
- **Progress** is surfaced via an optional `tqdm` bar when running interactively.

## 10. Error Handling

- Each adapter implements exponential backoff with jitter on 429/503 responses.
- Per-source failures are non-fatal: the result set includes a `warnings` field listing sources that errored.
- `RateLimitError` is raised (not swallowed) if all retries are exhausted.
- Google Scholar adapter includes configurable delay and optional proxy support.

## 11. Configuration

```python
config = {
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
| `rapidfuzz` | Fuzzy title matching for dedup fallback |
| `bibtexparser` | BibTeX output formatting |
| `pandas` | CSV / long-format table |
| `pyarrow` | Parquet output (optional) |
| `pydantic` | Internal record schema validation |
| `tqdm` | Progress bars (optional) |
| `scholarly` | Google Scholar scraping (opt-in) |
| SQLite (stdlib) | Checkpoint store |

## 13. Resolved Design Decisions

Previously open questions from the original design, now decided:

- **Prefix expansion strategy** → Two-path: DataCite Events uses its native prefix filter; other adapters use a shared `PrefixEnumerator` backed by DataCite's `/dois` endpoint with disk caching.
- **Google Scholar status** → Retired from default source set; remains as opt-in.
- **Multi-source output** → Merged into a single result set with provenance preserved (`sources` list + long-format table).
- **Relation-type scope** → Include all six DataCite citation/reference relation types by default: `is-cited-by`, `is-referenced-by`, `is-supplement-to`, `cites`, `references`, `is-supplemented-by`. Rationale: the three "citation" types (dataset-side) and the three "reference" types (paper-side) describe the same underlying relationship from opposite directions. Critically, CrossRef-derived events in DataCite Event Data use `references`, so filtering to only `is-cited-by` would exclude the majority of real-world citations. Raw relation type is preserved per-record in the `relation_type` output column so downstream consumers can re-filter.
- **Async exposure** → Public API is sync-only. Adapters may use async HTTP internally (via `httpx.AsyncClient`) for concurrency within a run, but the three entry points (`find_citations`, `find_citations_batch`, `find_citations_by_prefix`) and `collapse_sources` are ordinary blocking functions. Rationale: keeps the library easy to use from scripts, notebooks, and non-async codebases; async concurrency is an implementation detail, not a user concern.
- **Output store format** → SQLite for the in-run checkpoint store (zero-dependency, native incremental writes, naturally supports the `(source, cited_doi)` resume key); Parquet or CSV for final export (Parquet preferred for large runs, CSV when downstream tools require it). BibTeX and JSON remain available as final export formats for the grouped-record shape.

## 14. Open Questions

1. **Versioned DOIs.** Many repositories (including EDI) issue per-version DOIs. A citation to v1 vs v2 is semantically distinct but often reflects the same underlying usage. Should a "concept DOI" rollup be provided as an optional post-processing step? Deferred — unclear whether versioned DOIs can be reliably distinguished across repositories; revisit after initial data is in hand.
2. **Dimensions batch size and rate limits.** Needs empirical testing once credentials exist.
3. **CrossRef adapter retention.** OpenCitations and DataCite Events both harvest CrossRef, so the standalone CrossRef adapter may be redundant. Test on a fixture of known-cited dataset DOIs: count edges found only by the CrossRef adapter (not propagated to OpenCitations or DataCite Events). If that count is negligible, demote CrossRef to opt-in.
4. **Semantic Scholar coverage of datasets.** Worth measuring on a fixture of known-cited dataset DOIs across repositories; may be weak enough to demote to opt-in.

## 15. Implementation Plan

1. `utils.normalize_doi()` + unit tests
2. `schema.CitationRecord` (pydantic) and long-format row types
3. `enumeration.PrefixEnumerator` — DataCite-backed DOI enumeration with disk cache
4. Existing adapters: OpenAlex, Semantic Scholar, CrossRef (reuse)
5. New adapters: `DataCiteEventsAdapter`, `OpenCitationsAdapter`, `DimensionsAdapter`
6. Merge/dedup engine (DOI-first, fuzzy fallback)
7. Formatters: JSON, CSV, BibTeX, `LongFormatFormatter` + `collapse_sources()`
8. Entry points: `find_citations`, `find_citations_batch`, `find_citations_by_prefix`
9. Checkpoint store (SQLite) and resume logic
10. Integration tests against ~20 known-cited dataset DOIs drawn from multiple repositories (EDI, Dryad, Zenodo, Dataverse) to validate generality
11. Scale test: full EDI prefix sweep end-to-end; record per-source latency, record counts, and overlap
12. README and example notebook

## 16. Future Work

- CLI interface wrapping the primary functions
- R package (e.g., `rcitations`) calling the Python library via `reticulate`
- Full-text mention detection for informal dataset citations
- Scopus and Web of Science adapters (when institutional access is available)

## 17. Deliverables

- Python library with adapters, merge engine, formatters, and batch/prefix entry points
- Checkpoint/resume support for long runs
- Example notebook demonstrating (a) a single-DOI lookup, (b) a batch run over a custom list, and (c) a full-repository sweep via prefix
- This design document (committed alongside the library)
