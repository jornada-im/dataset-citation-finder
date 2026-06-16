# CLAUDE.md

Project context for Claude Code sessions on **dataset-citation-finder**.

## What this project is

A Python library for discovering academic papers that cite a given dataset, by querying multiple scholarly databases and merging results with provenance preserved. Repository-agnostic — accepts a single DOI, an arbitrary list, or a DOI prefix (e.g., `10.6073` to sweep the entire Environmental Data Initiative catalog).

The canonical source of truth for design decisions is **`docs/dataset_citation_finder_design.md`** — read it first.

## Architecture (one-paragraph version)

Four layers: **source adapters → SQLite store → coalesce → exporters.** Adapters share a common `SourceAdapter` interface and emit `LongRow` records. For batch and prefix runs, the engine streams those rows directly into per-source raw tables in a SQLite store (`source_datacite_events`, `source_openalex`, `source_opencitations`); a `coalesce` step then rebuilds a canonical `citations` table with `sources` and `relation_types` aggregated per `(cited_doi, citing_doi)` edge. For single-DOI lookups the in-memory path (DataFrame / GroupedRecord) is preserved. Public API is sync-only; async is an internal concurrency mechanism. DOI normalization happens at every adapter's output boundary so dedup keys are reliable.

## Current implementation status

**Implemented:**
- `utils.normalize_doi` — canonical DOI form
- `schema.LongRow` / `schema.GroupedRecord` — pydantic models with DOI validation
- `http_client` — polite-pool User-Agent, exponential backoff with jitter, `Retry-After` respect
- `enumeration.PrefixEnumerator` — DataCite-backed DOI enumeration with disk cache
- `engine.collect_rows` (in-memory) and `engine.collect_to_store` (SQLite-backed)
- `collapse.collapse_citations_df` — in-memory long DataFrame → deduplicated view
- `store/` — schema v2 DDL with `meta`/`runs`/`cited_dois`/`queried_dois`/`doi_validations`/`source_*`/`citations` tables; `open_store` with `application_id` validation, schema-version checks, and forward-only migrations (m001 initial, m002 cited_dois); per-source upsert helpers; SQLite-backed `coalesce_citations`; read-only `describe_store`.
- `validation.validate_dois_async` (+ public `validate_dois`) — DOI handle resolver + optional DataCite/CrossRef registrar probes; in-memory or persisted into `cited_dois` + `doi_validations`.
- `adapters.base.SourceAdapter` — base ABC + default `query_batch` fanout
- `adapters.datacite_events.DataCiteEventsAdapter` — full, per-DOI and prefix
- `adapters.opencitations.OpenCitationsAdapter` — full per-DOI
- `adapters.openalex.OpenAlexAdapter` — full, with DOI → work-ID resolution and cursor pagination
- Public API: `find_citations`, `find_citations_batch`, `find_citations_by_prefix`, `validate_dois`, `coalesce_citations`, `read_citations`, `export_citations`, `open_store`, `describe_store`, `collapse_citations_df`. `find_citations` and `find_citations_batch` accept an optional `db_path` (without it: in-memory DataFrame). `find_citations_by_prefix` requires `db_path`. `validate_dois` is decoupled from the citation pipeline and persists results into `cited_dois` (latest status) and `doi_validations` (audit log).
- `api_keys.resolve_api_keys` — merges an explicit `api_keys={"openalex": ..., "opencitations": ...}` dict with `<SOURCE>_API_KEY` env-var fallbacks (explicit wins); resolved keys are passed to adapter constructors via `api_key`. Currently wired for OpenAlex (`api_key` query param) and OpenCitations (`authorization` header, no `Bearer` prefix).

**Stubs that raise `NotImplementedError`:**
- `adapters.stubs.SemanticScholarAdapter`
- `adapters.stubs.CrossRefAdapter`
- `adapters.stubs.DimensionsAdapter` (also: only registers if credentials detected)
- `adapters.stubs.GoogleScholarAdapter` (opt-in only)

**Not yet built:**
- BibTeX export
- Live integration tests against real APIs
- Incremental coalesce on dirty pairs (currently truncate-and-rebuild)

## Open design questions (still unresolved)

From design doc §14:

1. **Versioned DOIs** — many repositories issue per-version DOIs; whether to collapse to a "concept DOI" is deferred until we have enough data to know if it's possible to detect reliably across repositories.
2. **Dimensions batch size and rate limits** — needs empirical testing; the adapter is a stub anyway.
3. **CrossRef adapter retention** — OpenCitations and DataCite Events both harvest CrossRef. Test whether the standalone CrossRef adapter contributes edges the others miss; if not, demote to opt-in.
4. **Semantic Scholar coverage of datasets** — measure on a fixture of known-cited dataset DOIs across multiple repositories; may be weak enough to demote to opt-in.

## Operational notes (learned from running against EDI)

- **DataCite Events `page_size=1000` causes read timeouts on prefix sweeps.** EDI's `10.6073` prefix has tens of thousands of citation events. At 1000 events/page, response generation can exceed 30s and trigger `httpx.ReadTimeout`. **For now leave `page_size` at 1000, but bumped `DEFAULT_TIMEOUT` to 60s.** Both should be exposed through the public API in a follow-up.
- **`enumeration.py` `page_size` is unrelated** — that endpoint returns small uniform rows and is not the bottleneck. Leave at 1000.
- **OpenCitations adapter must call `api.opencitations.net`, not `opencitations.net`.** The legacy `opencitations.net/index/api/v2/...` path 301-redirects to the canonical `api.opencitations.net` host, and httpx drops the `Authorization` header on cross-host redirects — silently sending API-keyed requests as anonymous. `BASE_URL` already points at the canonical host; don't revert it.
- **`http_client.RetryPolicy.max_retry_after` (120s) caps how long we'll honor a server's `Retry-After`.** OpenAlex IP-level throttling has been observed to return `Retry-After` values up to ~19h; honoring that literally would stall a sweep indefinitely. Past the cap, the 429/503 response is surfaced to the caller instead of sleeping.
- **`OpenAlexAdapter` is throttled like `OpenCitationsAdapter`** — `default_concurrency = 2`, `inter_request_delay = 0.5`. OpenAlex enforces a per-IP/per-key rate limit (~10 req/s) and each DOI costs 2+ requests (resolve + cited-by pagination); the old default (`concurrency=8`, no delay) burst past that limit and triggered the 429s described above. An `api_key` raises the ceiling but doesn't remove it.
- **Resume semantics live in the store now.** Each `(cited_doi, source)` pair queried bumps `queried_dois.last_queried_at`; passing `refresh_after=timedelta(...)` to a re-run skips pairs queried within the window. An interrupted run rolls back the per-run transaction (so writes are atomic per call) but re-running is the recovery path — there is no separate checkpoint file.
- **Per-DOI failures don't count as "queried."** If an adapter's `query_single` raises, or the response is HTTP >= 400 (other than 404), the base class records the DOI via `_mark_failed` / `take_failed_dois()`. The engine excludes those DOIs from `bump_queried_dois`, so a failed DOI is retried on the next run even if it falls inside the `refresh_after` window.
- **Adapter ordering in API layer is sequential per-source** — within `_run_to_store`, prefix-capable adapters run before per-DOI adapters, and per-DOI adapters are dispatched one at a time so the freshness filter can be applied per-source. If a single source stalls, that source's portion of the run stalls; others run normally if dispatched first.
- **DataCite Events `obj-id`/`subj-id` filters require the full `https://doi.org/<doi>` form** — a bare DOI (e.g. `10.6073/pasta/...`) silently matches zero events. This only affected `query_single` (used by `find_citations` / `find_citations_batch`); `query_prefix` uses the separate `prefix=` filter, which accepts a bare prefix and was unaffected. Fixed via `_doi_url()` in `adapters/datacite_events.py`.

## Decisions worth knowing about

These are settled but nontrivial — see design §13 for full reasoning.

- **All six DataCite citation/reference relation types are in the default set**, not just `is-cited-by`. CrossRef-derived events arrive as `references`, so filtering only `is-cited-by` would miss the bulk of real citations. Raw `relation_type` is preserved per-row for downstream filtering.
- **Provenance is first-class.** Per-source raw tables (`source_*`) are intentionally undeduplicated — one row per source-side observation. `coalesce_citations()` (or `collapse_citations_df()` for the in-memory single-DOI path) produces the deduplicated view with `sources` and `relation_types` lists. Cross-source agreement is a signal, not noise.
- **Google Scholar is opt-in only** (in registry but not in `DEFAULT_SOURCES`).
- **Public API is sync-only.** Adapters use `httpx.AsyncClient` internally; the entry points wrap with `asyncio.run`.
- **Canonical output is a SQLite store.** Per-source raw tables + a coalesced `citations` table; CSV / Parquet are derived via `export_citations`. The store stamps `application_id = 0x44434646` plus a `meta` row with `schema_version` and `library_version`, so re-opening a foreign or older file is safe (raises `StoreFormatError` / `SchemaVersionError`, or auto-migrates).
- **`cited_dois` is separate from `queried_dois`.** `cited_dois` is the in-scope DOI registry — every DOI the user submitted as input or that prefix enumeration surfaced, regardless of whether any source returned citations for it. `queried_dois` is the per-`(cited_doi, source)` freshness ledger that drives `refresh_after`. The "uncited DOIs" question is `LEFT JOIN cited_dois LEFT JOIN citations`.
- **DOI validation is opt-in and decoupled.** `validate_dois` is a separate function; the citation entry points never call it. Validation results persist into `cited_dois.validation_status` (latest) and `doi_validations` (per-event audit log).
- **API keys: explicit dict beats env vars, per source.** `find_citations*` / `validate_dois` accept `api_keys={...}`; `api_keys.resolve_api_keys` fills in any missing sources from `OPENALEX_API_KEY`, `OPENCITATIONS_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `DIMENSIONS_API_KEY`. Adapters that don't take a key (DataCite Events) silently ignore entries in the dict.
- **OpenAlex rate limits: `429` (daily cap) and `403` (per-second) are handled differently — on purpose.** A `429` means the daily budget is spent and won't clear within the run, so it's dropped from the adapter's retry set and converted to `BudgetExhausted` (see `adapters/base.py`), which stops the OpenAlex phase cleanly and leaves unfinished DOIs out of `queried_dois` for a `refresh_after` re-run. A `403` (per-second throttle) is left as a plain per-DOI failure (mark failed, retry next run) — **do not add `403` to OpenAlex's retry set.** Retrying it would turn a persistent `403` (invalid/expired key, IP block) into ~15s of backoff *per DOI*, making a whole sweep crawl; and with concurrency=2 + 0.5s delay we stay under the ~10/s ceiling so per-second `403`s should be rare anyway. Also: resolve via the per-DOI `/works/doi:` endpoint, never the `filter=doi:` batch form — the latter silently misses DataCite-sourced dataset works (e.g. EDI `10.6073`) that resolve fine per-DOI.

## Project conventions

- **Python 3.10+**. Type hints throughout (using `from __future__ import annotations` at the top of every module).
- **Tests** in `tests/`, mirroring source layout. Adapter HTTP is mocked with `httpx.MockTransport` (see `tests/conftest.py` for the factory). API-layer tests monkey-patch the adapter `REGISTRY` with fakes — see `tests/test_api.py` for the pattern.
- **Imports**: stdlib first, third-party second, local last; one blank line between groups.
- **Logging**: each module gets `log = logging.getLogger(__name__)`. Adapter failures log at `WARNING`/`ERROR` and don't propagate — the engine isolates per-adapter exceptions so one broken source doesn't kill a run.
- **No CLI yet, no R bindings yet.** Library-first by design (see design §3 non-goals and §16 future work).

## How to run things

```bash
# Install (editable, with dev deps)
uv sync --extra dev   # or: pip install -e ".[dev]"

# Run all tests
uv run pytest         # or: python -m pytest

# Single-DOI in-memory check
python -c "from dataset_citation_finder import find_citations; \
           print(find_citations('10.6073/pasta/<your-doi>', \
                                sources=['datacite_events', 'openalex', 'opencitations'], \
                                email='you@example.org'))"

# Build / update a SQLite store
python -c "from dataset_citation_finder import find_citations_batch; \
           s = find_citations_batch(['10.6073/pasta/<doi>'], 'citations.sqlite', \
                                    email='you@example.org'); \
           print(s.run_id, s.total_rows_written, s.coalesced)"
```

## Where to look first

- New adapter? → mirror `adapters/openalex.py` or `adapters/opencitations.py` (per-DOI) or `adapters/datacite_events.py` (prefix-capable). Add to `REGISTRY` in `adapters/__init__.py`. Add to `DEFAULT_SOURCES` only if it's enabled by default. Then add: a per-source table via a new migration in `store/migrations/`, an entry in `store.schema.SOURCE_TABLES`, and a per-source upsert function in `store.upsert.UPSERTERS`.
- Public API change? → `api.py`. Keep entry points sync-wrapping `_run_to_store` / `_run_in_memory`. `find_citations_batch` and `find_citations_by_prefix` require `db_path`; `find_citations` accepts an optional one.
- In-memory record schema change? → `schema.py` (LongRow / GroupedRecord). DOI fields are validated by `normalize_doi` at the boundary — keep that invariant.
- Store schema change? → add a migration to `store/migrations/` (numbered, idempotent, declares `from_version` / `to_version`), bump `store.schema.CURRENT_SCHEMA_VERSION`, and update DDL constants in `store.schema`. **Never** edit an existing migration in-place after it has been released — write a new one.
- Export format change? → `api.export_citations`. New formatters can live alongside it; the in-memory `formatters/` directory is reserved for future BibTeX/JSON output of grouped records.
- About to run a real prefix sweep (e.g. against EDI)? → `docs/user_guide.md` has the practical first-run checklist (`email`, `cache_dir`, expected runtime, `sources=` to silence stub warnings).

## What not to do

- Don't add new public entry points unless they map cleanly to the input shapes (single, batch, prefix). Variants with different output formats should be parameters, not new functions.
- Don't reach for fuzzy title matching as a primary dedup mechanism — DOI-keyed dedup is the contract. Fuzzy is a fallback only when DOIs are absent (and isn't implemented yet).
- Don't expose async to users. Keep `httpx.AsyncClient` and `asyncio` internal.
- Don't return search results / scrape Google Scholar by default. Keep it opt-in.
