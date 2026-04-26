# CLAUDE.md

Project context for Claude Code sessions on **dataset-citation-finder**.

## What this project is

A Python library for discovering academic papers that cite a given dataset, by querying multiple scholarly databases and merging results with provenance preserved. Repository-agnostic — accepts a single DOI, an arbitrary list, or a DOI prefix (e.g., `10.6073` to sweep the entire Environmental Data Initiative catalog).

The canonical source of truth for design decisions is **`docs/dataset_citation_finder_design.md`** — read it first.

## Architecture (one-paragraph version)

Three layers: **source adapters → merge/dedup engine → output formatters.** Adapters share a common `SourceAdapter` interface and emit `LongRow` records (one per source attribution, intentionally undeduplicated). The engine collects rows from configured adapters and produces either a long-format pandas DataFrame or a list of `GroupedRecord` objects (merged by citing DOI). Public API is sync-only; async is an internal concurrency mechanism, not a user-facing concern. DOI normalization happens at every adapter's output boundary so dedup keys are reliable regardless of upstream formatting.

## Current implementation status

**Implemented and tested (81 unit tests, all passing):**
- `utils.normalize_doi` — canonical DOI form
- `schema.LongRow` / `schema.GroupedRecord` — pydantic models with DOI validation
- `http_client` — polite-pool User-Agent, exponential backoff with jitter, `Retry-After` respect
- `enumeration.PrefixEnumerator` — DataCite-backed DOI enumeration with disk cache
- `engine` — adapter orchestration with per-adapter exception isolation
- `collapse.collapse_sources` — long-format → deduplicated view
- `adapters.base.SourceAdapter` — base ABC + default `query_batch` fanout
- `adapters.datacite_events.DataCiteEventsAdapter` — full implementation, both per-DOI and prefix paths
- `adapters.opencitations.OpenCitationsAdapter` — full per-DOI implementation
- `adapters.openalex.OpenAlexAdapter` — full implementation with DOI → work-ID resolution and cursor pagination
- Public API: `find_citations`, `find_citations_batch`, `find_citations_by_prefix`, `collapse_sources`

**Stubs that raise `NotImplementedError`:**
- `adapters.stubs.SemanticScholarAdapter`
- `adapters.stubs.CrossRefAdapter`
- `adapters.stubs.DimensionsAdapter` (also: only registers if credentials detected)
- `adapters.stubs.GoogleScholarAdapter` (opt-in only)

These are in the default source list, so a default `find_citations(doi)` call logs two non-fatal `NotImplementedError` warnings (Semantic Scholar, CrossRef) per DOI and continues with the three working adapters. Pass an explicit `sources=[...]` list to silence.

**Not yet built:**
- SQLite checkpoint/resume store for long-running prefix sweeps (design §6, §15 step 9)
- BibTeX file-output formatter (design §4.3)
- CSV file-output formatter as a first-class output (the long-format DataFrame supports `.to_csv` already)
- Live integration tests against real APIs

## Open design questions (still unresolved)

From design doc §14:

1. **Versioned DOIs** — many repositories issue per-version DOIs; whether to collapse to a "concept DOI" is deferred until we have enough data to know if it's possible to detect reliably across repositories.
2. **Dimensions batch size and rate limits** — needs empirical testing; the adapter is a stub anyway.
3. **CrossRef adapter retention** — OpenCitations and DataCite Events both harvest CrossRef. Test whether the standalone CrossRef adapter contributes edges the others miss; if not, demote to opt-in.
4. **Semantic Scholar coverage of datasets** — measure on a fixture of known-cited dataset DOIs across multiple repositories; may be weak enough to demote to opt-in.

## Operational notes (learned from running against EDI)

- **DataCite Events `page_size=1000` causes read timeouts on prefix sweeps.** EDI's `10.6073` prefix has tens of thousands of citation events. At 1000 events/page, response generation can exceed 30s and trigger `httpx.ReadTimeout`. **For now leave `page_size` at 1000, but bumped `DEFAULT_TIMEOUT` to 60s.** Both should be exposed through the public API in a follow-up.
- **`enumeration.py` `page_size` is unrelated** — that endpoint returns small uniform rows and is not the bottleneck. Leave at 1000.
- **One failed page = lost data.** When a page times out beyond max retries, the cursor stream ends because the failing page also held the `next` link. Recovery requires either re-running with smaller pages, or falling back to `find_citations_batch` with the per-DOI path. A proper checkpoint/resume layer would prevent this entirely (design §6).
- **Adapter ordering in API layer is sequential** — DataCite prefix path runs to completion before per-DOI adapters start. If DataCite stalls, the whole run stalls. Worth revisiting if checkpoint/resume is added.

## Decisions worth knowing about

These are settled but nontrivial — see design §13 for full reasoning.

- **All six DataCite citation/reference relation types are in the default set**, not just `is-cited-by`. CrossRef-derived events arrive as `references`, so filtering only `is-cited-by` would miss the bulk of real citations. Raw `relation_type` is preserved per-row for downstream filtering.
- **Provenance is first-class.** The long-format table is intentionally undeduplicated — one row per `(cited_doi, citing_doi, source)` tuple. `collapse_sources()` produces the deduplicated view with `sources` and `relation_types` lists. Cross-source agreement is a signal, not noise.
- **Google Scholar is opt-in only** (in registry but not in `DEFAULT_SOURCES`).
- **Public API is sync-only.** Adapters use `httpx.AsyncClient` internally; the entry points wrap with `asyncio.run`.
- **Output store format:** SQLite for the in-run checkpoint (when implemented); Parquet/CSV for export.

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

# Run a quick end-to-end check
python -c "from dataset_citation_finder import find_citations; \
           print(find_citations('10.6073/pasta/<your-doi>', \
                                sources=['datacite_events', 'openalex', 'opencitations'], \
                                email='you@example.org'))"
```

## Where to look first

- New adapter? → mirror `adapters/openalex.py` or `adapters/opencitations.py` (per-DOI) or `adapters/datacite_events.py` (prefix-capable). Add to `REGISTRY` in `adapters/__init__.py`. Add to `DEFAULT_SOURCES` only if it's enabled by default.
- Public API change? → `api.py`. Keep it sync-wrapping `_pipeline()`.
- Schema change? → `schema.py` (LongRow / GroupedRecord). DOI fields are validated by `normalize_doi` at the boundary — keep that invariant.
- Output format change? → currently long-format DataFrame and grouped records are produced in `engine.py`. New file-output formatters should live in `formatters/`.

## What not to do

- Don't add new public entry points unless they map cleanly to the input shapes (single, batch, prefix). Variants with different output formats should be parameters, not new functions.
- Don't reach for fuzzy title matching as a primary dedup mechanism — DOI-keyed dedup is the contract. Fuzzy is a fallback only when DOIs are absent (and isn't implemented yet).
- Don't expose async to users. Keep `httpx.AsyncClient` and `asyncio` internal.
- Don't return search results / scrape Google Scholar by default. Keep it opt-in.
