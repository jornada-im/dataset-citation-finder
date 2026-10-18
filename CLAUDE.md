# CLAUDE.md

Project context for Claude Code sessions on **dataset-citation-finder**.

## What this project is

A small Python library that finds papers citing dataset DOIs by querying DataCite Event Data, OpenAlex and OpenCitations, then merging the results with per-source provenance. The priority is **ease of use and readability for newcomers** who want citation lists for a handful to ~1,000 DOIs; large, periodically refreshed lists are supported through an optional SQLite store.

`README.md` is the user-facing contract. `docs/user_guide.md` covers large runs. `docs/dataset_citation_finder_design.md` is the **historical** design for v0.3 (adapters, async engine, migrations, prefix sweeps). v0.4 deliberately simplified it, so where they disagree, the code and this file win.

## Architecture

Six modules, no subpackages:

- `api.py` — public functions `find_citations`, `read_citations`, `dois_under_prefix`, plus `collapse` (long rows → one row per `(cited_doi, citing_doi)` with `;`-joined `sources` / `relation_types`).
- `sources.py` — one `fetch_<source>(client, doi) -> list[dict]` function per database, and the `SOURCES` registry (`Source(fetch, delay, api_key_env)`). Rows are plain dicts: `cited_doi`, `citing_doi`, `source`, `relation_type` (`""` when the source has none).
- `store.py` — optional SQLite store: `citations` (PK `cited_doi, citing_doi, source, relation_type`, with `first_seen` / `last_seen`) and `queried` (one row per `(cited_doi, source)` for every input DOI; `queried_at` is NULL until that source answers, and drives `refresh_after`). `read_citations(db, table="queried")` exposes it. Version lives in `PRAGMA user_version` (currently 2). `open_store` upgrades v1 stores in place, because v1 had `queried_at NOT NULL`.
- `http_client.py` — `make_client(email)` and `get(...)` with backoff/Retry-After handling.
- `validation.py` — `validate_dois` (doi.org HEAD + optional DataCite/CrossRef registrar probe). Returns a DataFrame; never touches the store; `find_citations` never calls it.
- `utils.py` — `normalize_doi`, applied to every input DOI and every citing DOI.

Execution model: **synchronous, no asyncio.** `find_citations` runs each source in its own thread (`ThreadPoolExecutor`, one worker per source); inside a thread, DOIs are queried one at a time with `Source.delay` between them. Each worker opens its own SQLite connection; results are committed after every DOI. A shared `threading.Event` stops all workers after their current DOI when any worker fails or the user hits Ctrl-C (`as_completed` surfaces the first failure).

## Behaviors that are load-bearing

- **`refresh_after` is the resume and refresh mechanism.** Every input DOI is recorded in `queried` up front with `queried_at` NULL, which keeps the scope list in the store. `queried_at` is set only after a successful fetch. Failed DOIs (exceptions, HTTP ≥ 400 other than 404) keep NULL or their last successful time, so a never-answered DOI is always re-queried, even inside the window. A 404 means "no citations", not failure.
- **Returned table with a store = everything stored for the input DOIs**, not just this run's results, so skipped-as-fresh DOIs still appear.
- **Failures are reported, not raised:** `df.attrs["failed_dois"] = {source: [dois]}`. One broken source never kills the others.
- **OpenAlex `429` vs `403` — handled differently on purpose.** `429` = daily budget spent; it's excluded from OpenAlex's retry statuses and raised as `BudgetExhausted`, which stops OpenAlex for the run and marks its remaining DOIs failed. `403` (per-second throttle or a bad key) is a plain per-DOI failure — **do not retry it**: a persistent 403 would add ~15s of backoff per DOI.
- **OpenAlex: resolve via `/works/doi:<doi>`, never `filter=doi:`.** The filter index silently misses DataCite-registered datasets (e.g. EDI `10.6073`). Skip the listing request when `cited_by_count == 0`.
- **DataCite Events `obj-id` / `subj-id` filters need `https://doi.org/<doi>`**; a bare DOI silently matches nothing.
- **All six DataCite relation types count as citations** (`cites`, `references`, `is-supplemented-by` with the dataset as object; `is-cited-by`, `is-referenced-by`, `is-supplement-to` with the dataset as subject). CrossRef-derived events arrive as `references`.
- **DataCite pagination:** take only the `page[cursor]` from `links.next` and keep our own params. Don't follow the `next` URL verbatim.
- **OpenCitations must call `api.opencitations.net`.** The legacy host redirects there, and httpx drops `Authorization` on cross-host redirects. The key is sent as a bare `authorization` header (no `Bearer`). OpenAlex's key is an `api_key` query param. `sources.add_api_key` sets both on the per-source client.
- **`http_client.MAX_RETRY_AFTER = 120s`.** OpenAlex has sent `Retry-After` values of ~19h; past the cap we return the response instead of sleeping.
- **Rate limits:** OpenAlex ~10 req/s per IP/key (we use one request at a time + 0.1s delay); OpenCitations is low for anonymous users (one request at a time + 0.2s). DataCite has no delay and is usually the slowest source (two requests per DOI), so it sets total run time. Run time is dominated by network latency, so keep timing claims in user docs relative to the user's connection rather than quoting fixed numbers.

## Measured (live, 2026-10-18, 71 `knb-lter-nes` DOIs, on the user's home network)

58 citations across 29 datasets, identical to the v0.3 results. A full run took 177–482s. The variation tracked network speed: all three services slowed together. Back to back, the old async version took 238–258s and the new one 177–270s, so their speeds are comparable. A Ctrl-C followed by a resume with `refresh_after` re-queried only the unfinished DOIs and produced an identical table. Changing DataCite `page[size]` (100 vs 1000) made no measurable difference.

## Project conventions

- **Python 3.10+**, `from __future__ import annotations` at the top of every module, type hints throughout.
- **Tests** in `tests/`, one file per module. HTTP is mocked with `httpx.MockTransport` (`mock_client` fixture in `tests/conftest.py`). `time.sleep` is patched to a no-op for every test (`no_sleep` fixture, which records requested durations). API tests replace each source's `fetch` via `dataclasses.replace(SOURCES[name], fetch=fake)` so the real `delay` / `api_key_env` are kept.
- **Imports**: stdlib, third-party, local, separated by blank lines.
- **Logging**: `log = logging.getLogger(__name__)` per module; per-DOI failures log at WARNING. User-facing progress goes to stderr via `print` (the `progress=` flag), not logging.
- **Dependencies**: `httpx` and `pandas` only. Keep it that way unless there's a strong reason.

## How to run things

```bash
uv sync --extra dev          # or: pip install -e ".[dev]"
uv run pytest
uv run python -c "from dataset_citation_finder import find_citations; \
    print(find_citations('10.6073/pasta/<doi>', email='you@example.org'))"
```

## Where to look first

- New source? → write `fetch_<name>(client, doi)` in `sources.py` (return `[]` for "not found", raise on failure, build rows with `_row`), register it in `SOURCES`, and handle its key in `add_api_key` if it takes one. Nothing else changes: the store is source-agnostic.
- Store schema change? → edit `_SCHEMA`, bump `STORE_VERSION` in `store.py`, and add an upgrade script from the previous version (see `_UPGRADE_FROM_1` and the `version == 1` branch in `open_store`). The user has real stores, so don't break them. Files from v0.3 (version 0 with tables) still raise `StoreError`.
- Output columns? → `api.COLUMNS` and `api.collapse`. Output stays a DataFrame; users write CSV with `df.to_csv`.

## Open questions

1. **Versioned DOIs** — many repositories issue per-version DOIs (EDI mints one per package revision). Whether to roll them up to a "concept" DOI is unresolved.
2. **Speed for very large lists** — DataCite dominates run time. If needed, give DataCite a small pool of workers (it has no tight rate limit); keep OpenAlex and OpenCitations at one request at a time.
3. **More sources** (Semantic Scholar, CrossRef, Dimensions) — not implemented. Before adding one, measure whether it finds citations the current three miss.

## What not to do

- Don't reintroduce asyncio, an adapter class hierarchy, or a migrations framework without a measured need; readability for newcomers is the priority.
- Don't add new public functions for variants of the same task. Prefer a parameter on `find_citations`.
- Don't dedup by fuzzy title matching. DOI-keyed dedup is the contract.
- Don't send the user's email anywhere except the `User-Agent` of requests they asked for.
