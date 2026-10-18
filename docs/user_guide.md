# Running large lists (thousands of DOIs)

The README covers the basics. These notes are for big runs, such as a whole
EDI scope or prefix.

- **Pass `email="..."`.** DataCite, OpenAlex and CrossRef ask clients to
  identify themselves, and anonymous clients get less tolerance.
- **Always use `db_path` and `refresh_after`.** Results are saved after every
  DOI, so an interrupted run loses almost nothing, and re-running the same
  call resumes it.
- **Expect a long run.** The three databases are queried in parallel, but
  each one is asked about one DOI at a time, and DataCite (two requests per
  DOI) is usually the slowest. Most of the time is spent waiting for
  responses, so speed depends mainly on your internet connection. On a slow
  home network, expect up to a few seconds per DOI, so thousands of DOIs can
  take hours. It's fine to stop and resume.
- **Save prefix listings.** `dois_under_prefix("10.6073")` makes hundreds of
  requests. Write the list to a file and reuse it until you want to pick up
  newly registered DOIs.

A reasonable first invocation:

```python
from datetime import timedelta
from pathlib import Path
from dataset_citation_finder import dois_under_prefix, find_citations

path = Path("edi_dois.txt")
if not path.exists():
    path.write_text("\n".join(dois_under_prefix("10.6073", email="you@example.org")))

df = find_citations(
    path.read_text().split(),
    db_path="edi_citations.sqlite",
    refresh_after=timedelta(days=30),
    email="you@example.org",
    api_keys={"openalex": "..."},
)
df.to_csv("edi_citations.csv", index=False)
```

To dry-run on a smaller set first, try one EDI scope (see
`demo/fetch_scope_dois.py`) or the first few hundred DOIs of the list.

## OpenAlex's daily budget and multi-day runs

OpenAlex enforces a **daily request budget** that resets at midnight UTC. A
large run can use it up, especially without an API key.

- **Set an OpenAlex API key.** The budget with a key is far larger. Pass
  `api_keys={"openalex": "..."}` or set `OPENALEX_API_KEY`.
- **When the budget runs out**, the library prints
  `openalex: OpenAlex daily request budget is used up ...`, stops querying
  OpenAlex, and lets the other databases finish. OpenAlex's unfinished DOIs
  are listed in `df.attrs["failed_dois"]["openalex"]` and are not marked as
  queried.

To continue the next day, re-run the same call. `refresh_after` skips the
DOIs OpenAlex already answered, so only the unfinished ones are queried.
Re-fetched citations update in place; resuming never duplicates rows.

## Interrupting a run

Press Ctrl-C at any time. Each database finishes the DOI it is working on
(usually a few seconds) and then stops. Everything answered so far is
already saved.
