
## First run of `find_citations_by_prefix`

Before you try find_citations_by_prefix against a real DOI prefix:

  - Pass email="..." — DataCite's polite-pool tolerance for
  unidentified clients on prefix queries is much lower than
  for per-DOI queries.
  - Pass cache_dir="~/.cache/dcf" — PrefixEnumerator caches
  the DOI list for 24h. Without a cache_dir the enumeration is
   re-fetched on every run, which for EDI's 10.6073 is
  hundreds of paginated requests.
  - Expect a slow first run — for a large prefix like 10.6073,
   DataCite Events' prefix-native query and OpenCitations'
  per-DOI fanout dominate runtime; OpenCitationsAdapter is
  throttled to default_concurrency=2 with a 0.5s inter-request
   delay (per the operational notes in CLAUDE.md, this avoids
  429s on long sweeps).
  - The default source set is the three implemented adapters
  (datacite_events, openalex, opencitations); the Semantic
  Scholar and CrossRef stubs are not run by default. Pass
  sources=[...] only to narrow further or to opt into a stub.
  - Resumability — if the run is interrupted, the per-run
  transaction rolls back, but cited_dois upserts from the
  enumeration (which happens before adapter dispatch) are
  committed earlier in the transaction lifecycle. Re-invoking
  with refresh_after=timedelta(days=1) will skip pairs already
   processed.

  A reasonable first invocation:

  from dataset_citation_finder import find_citations_by_prefix
  summary = find_citations_by_prefix(
      "10.6073",
      db_path="edi_citations.sqlite",
      sources=["datacite_events", "openalex",
  "opencitations"],
      email="gmaurer@nmsu.com",
      cache_dir="~/.cache/dcf",
  )


    summary = find_citations_by_prefix("10.6073",
        db_path="edi_citations_20260615.sqlite",
        sources=["datacite_events", "openalex","opencitations"],
        api_keys={"openalex":<"key">, "opencitations":<"key">},
        email="gmaurer@nmsu.com",
        cache_dir="~/.cache/dcf"
    )

  print(summary)

  If you want to dry-run on a smaller prefix first to validate
   the pipeline end-to-end, 10.5281/zenodo (Zenodo) and
  10.5061/dryad (Dryad) are both valid registrant prefixes —
  though 10.5281 and 10.5061 are the actual DOI prefixes (the
  registrant level), so try those.

## OpenAlex daily budget and multi-day resume

OpenAlex enforces a **daily request budget** (it resets at midnight UTC), and
a full sweep of a large prefix like EDI's `10.6073` can exhaust it in a single
run. Two things to know:

- **Set an OpenAlex API key.** The daily budget with a key is far larger than
  without one. Pass `api_keys={"openalex": "..."}` or set `OPENALEX_API_KEY`
  in the environment. This alone may be enough for smaller prefixes.

- **When the budget is hit, the run stops the OpenAlex phase cleanly** rather
  than hammering the API with doomed requests. It logs a warning like
  `openalex: daily request budget exhausted (HTTP 429) … resets in ~NNNNNs`,
  finishes the other sources, and returns normally. Every OpenAlex DOI that
  did *not* complete is left out of the freshness ledger so it will be retried.
  (DataCite Events and OpenCitations are unaffected — they have their own
  pacing.)

To **resume the next day**, re-run the *same* command with `refresh_after`
set. The freshness filter then skips the DOIs OpenAlex already answered and
re-queries only the ones left unfinished:

```python
from datetime import timedelta

# Re-run daily until the sweep completes. refresh_after skips DOIs queried
# within the window (the completed ones) and retries the rest.
summary = find_citations_by_prefix(
    "10.6073",
    db_path="edi_citations.sqlite",
    sources=["datacite_events", "openalex", "opencitations"],
    email="you@example.org",
    cache_dir="~/.cache/dcf",
    refresh_after=timedelta(hours=20),
)
```

Without `refresh_after` the re-run still works (unfinished DOIs are always
retried), but it will needlessly re-query the already-completed ones. Re-fetched
rows overwrite in place — `source_openalex` upserts are keyed on
`(cited_doi, citing_doi)` — so resuming never duplicates data.