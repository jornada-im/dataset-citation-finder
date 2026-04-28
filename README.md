# Dataset Citation Finder

A Python library for discovering academic papers that cite a given dataset,
across multiple scholarly databases and citation graphs.

Results for non-trivial scopes (a repository, a research program) are
persisted to a **SQLite database** with one raw table per source plus a
coalesced `citations` table. The same database file can be re-opened to
update incrementally.

See `docs/dataset_citation_finder_design.md` for the full design.

## Status

Implemented and tested:

- `utils.normalize_doi` — canonical DOI form
- `schema.LongRow` / `schema.GroupedRecord` — pydantic models
- Adapters (working): **DataCite Event Data**, **OpenCitations**, **OpenAlex**
- Adapters (stubs): Semantic Scholar, CrossRef, Dimensions, Google Scholar
- SQLite store layer: schema versioning, `application_id` validation,
  forward-only migrations, per-source upserts, `coalesce_citations`,
  `cited_dois` in-scope registry, `doi_validations` audit log
- `engine.collect_rows` (in-memory) and `engine.collect_to_store` (SQLite)
- `enumeration.PrefixEnumerator` — DataCite-backed DOI enumeration with disk cache
- `validate_dois` — optional DOI registration / resolvability check
- `http_client` — polite-pool User-Agent, exponential backoff, `Retry-After` respect
- Public API: `find_citations`, `find_citations_batch`, `find_citations_by_prefix`,
  `validate_dois`, `coalesce_citations`, `read_citations`, `export_citations`,
  `open_store`, `describe_store`, `collapse_citations_df`

Not yet implemented:

- Remaining adapters (Semantic Scholar, CrossRef, Dimensions, Google Scholar)
- BibTeX export formatter
- Live integration tests against real APIs

## Install

```bash
pip install -e .
```

Optional extras:

```bash
pip install -e ".[parquet]"    # pyarrow for Parquet export
pip install -e ".[bibtex]"     # bibtexparser (once BibTeX export is added)
pip install -e ".[progress]"   # tqdm progress bars
pip install -e ".[dev]"        # pytest, pytest-asyncio
```

## Usage

### Single-DOI lookup (in-memory)

```python
from dataset_citation_finder import find_citations

df = find_citations("10.6073/pasta/abc123", email="you@example.org")
```

`find_citations` returns a long-format DataFrame by default. Pass
`db_path=...` to also write the result through to a SQLite store.

### API keys (optional but recommended for OpenAlex and OpenCitations)

Several sources offer free API keys that grant higher rate limits. Pass them
explicitly:

```python
find_citations_batch(
    dois,
    db_path="store.sqlite",
    email="you@example.org",
    api_keys={
        "openalex": "your-openalex-key",
        "opencitations": "your-opencitations-token",
    },
)
```

Or set environment variables and the library picks them up automatically:

```bash
export OPENALEX_API_KEY=...
export OPENCITATIONS_API_KEY=...
export SEMANTIC_SCHOLAR_API_KEY=...
```

Recognized source-id → env var mappings live in
`dataset_citation_finder.api_keys`. Inspect what would be picked up:

```python
from dataset_citation_finder import resolve_api_keys
print(resolve_api_keys())
```

Adapters that don't take a key (DataCite Events) silently ignore entries
in the dict.

**How each key is sent.** OpenAlex uses an `api_key=...` query parameter on
every request. OpenCitations uses an `authorization: <token>` header (bare
token, no `Bearer ` prefix), per
<https://api.opencitations.net/index/v2#description>.

### Build (or update) a SQLite store from a list of DOIs

```python
from dataset_citation_finder import find_citations_batch

summary = find_citations_batch(
    ["10.6073/pasta/abc", "10.5281/zenodo.1234567"],
    db_path="citations.sqlite",
    email="you@example.org",
)
print(summary.run_id, summary.total_rows_written, summary.coalesced)
```

`db_path` is optional for batch lookups. Without it, `find_citations_batch`
runs the in-memory pipeline and returns a long-format DataFrame — the same
behavior as `find_citations`. For lists above ~100 DOIs the library logs a
recommendation to supply `db_path` for resumability and the freshness filter.

### Validate that DOIs are registered and resolvable

```python
from dataset_citation_finder import validate_dois

# In-memory: returns a DataFrame.
df = validate_dois(["10.6073/pasta/abc", "10.6073/missing-doi"])

# Persisted: writes results into the store's cited_dois + doi_validations
# tables and returns a RunSummary. Honors refresh_after for re-validation.
summary = validate_dois(
    ["10.6073/pasta/abc"],
    db_path="citations.sqlite",
    email="you@example.org",
)
```

Validation is decoupled from citation fetching — `find_citations*` never
calls it. A typical workflow audits DOIs first, then queries only the
resolvable ones:

```python
validate_dois(dois, db_path="store.sqlite")
df = read_citations("store.sqlite", view="cited_dois")
good = df[df["validation_status"] == "registered"]["cited_doi"].tolist()
find_citations_batch(good, db_path="store.sqlite")
```

### Full-repository sweep by DOI prefix

```python
from dataset_citation_finder import find_citations_by_prefix

summary = find_citations_by_prefix(
    "10.6073",
    db_path="edi_citations.sqlite",
    email="you@example.org",
    cache_dir="~/.cache/dcf",
)
```

### Periodic refresh against an existing store

The same call against an existing `db_path` updates it in place. Pass
`refresh_after` to skip pairs that have been queried recently:

```python
from datetime import timedelta

# Re-query only DOIs not seen in the last 30 days.
summary = find_citations_by_prefix(
    "10.6073",
    db_path="edi_citations.sqlite",
    refresh_after=timedelta(days=30),
)
```

`first_retrieved_at` is preserved on existing rows; only `last_retrieved_at`
and `run_id` advance.

### Read from the store

```python
from dataset_citation_finder import read_citations

# Coalesced canonical edges (one row per (cited_doi, citing_doi)).
df = read_citations("edi_citations.sqlite", view="coalesced")

# Long-format union across all source tables.
long_df = read_citations("edi_citations.sqlite", view="long")

# Single-source raw table.
oc_df = read_citations("edi_citations.sqlite", view="opencitations")

# Every DOI ever in scope, including DOIs that are not (yet) cited.
all_dois = read_citations("edi_citations.sqlite", view="cited_dois")

# Per-event audit log of validate_dois runs.
validations = read_citations("edi_citations.sqlite", view="doi_validations")
```

### Export to flat files

```python
from dataset_citation_finder import export_citations

export_citations("edi_citations.sqlite", "edges.csv")
export_citations("edi_citations.sqlite", "edges.parquet", format="parquet")
```

### Inspect a store

```python
from dataset_citation_finder import describe_store

info = describe_store("edi_citations.sqlite")
print(info.schema_version, info.run_count, info.citations_count)
print(info.rows_per_source)
```

### Working with a database produced by an older library version

`open_store` (called internally by every entry point) validates the file's
`application_id` and `meta.schema_version`. If the file is older than
the current schema, migrations run automatically. If it was written by a
newer library version, `SchemaVersionError` is raised. If the file is a
SQLite database that was *not* produced by this library, `StoreFormatError`
is raised.

## Run the tests

```bash
python -m pytest
```

## Database schema (high level)

- `meta` — schema version + library version stamps
- `runs` — one row per `find_citations*` / `validate_dois` invocation
- `cited_dois` — the in-scope DOI registry (every input/enumerated DOI, with optional validation status); independent of whether citations exist
- `queried_dois` — `(cited_doi, source) → last_queried_at` (drives `refresh_after` for citation fetches)
- `doi_validations` — append-only audit log of validation events
- `source_datacite_events`, `source_openalex`, `source_opencitations` — raw per-adapter tables
- `citations` — coalesced canonical edges, with `sources` and `relation_types` as JSON arrays

See design doc §7.2 for the full DDL.
