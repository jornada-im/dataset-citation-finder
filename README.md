# Dataset Citation Finder

Find the papers that cite your datasets. Give it one or more dataset DOIs and
it asks three citation databases, **DataCite Event Data**, **OpenAlex** and
**OpenCitations**, then merges the answers into one table that records which
database reported each citation.

```python
from dataset_citation_finder import find_citations

df = find_citations("10.6073/pasta/abc123", email="you@example.org")
```

| cited_doi | citing_doi | sources | source_count | relation_types | first_seen |
|---|---|---|---|---|---|
| 10.6073/pasta/abc123 | 10.1002/ecs2.1234 | datacite_events;openalex | 2 | references | 2026-10-18T13:30:00+00:00 |

No database finds every citation, and a citation reported by more than one
source is more trustworthy.

## Install

This project uses [uv](https://docs.astral.sh/uv/). The `uv.lock` file pins
exact package versions.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv (one-time)
uv sync                                            # create .venv with dependencies
uv sync --extra dev                                # ...plus pytest, to run the tests
```

Or, with pip: `pip install -e .` (or `pip install -e ".[dev]"`).

## A few DOIs (up to ~1,000)

```python
from dataset_citation_finder import find_citations

dois = open("my_dois.txt").read().split()   # one DOI per line
df = find_citations(dois, email="you@example.org")
df.to_csv("citations.csv", index=False)
```

`find_citations` accepts DOIs in any common form (`10.6073/...`,
`https://doi.org/10.6073/...`, `doi:10.6073/...`) and skips invalid ones with
a warning. It returns a pandas DataFrame with one row per (dataset, citing
paper):

| column | meaning |
|---|---|
| `cited_doi` | your dataset |
| `citing_doi` | the paper that cites it |
| `sources` | which databases reported the citation, `;`-separated |
| `source_count` | how many databases reported it |
| `relation_types` | how DataCite describes the link (`references`, `is-cited-by`, ...), if DataCite reported it |
| `first_seen` | when this library first found the citation (UTC) |

Datasets with no citations don't appear in the table. To list them:

```python
uncited = set(dois) - set(df["cited_doi"])
```

Progress lines are printed while it runs (`progress=False` silences them).
The three databases are queried at the same time, but each one is asked about
one DOI at a time to stay within its rate limits. Expect roughly 1–4 seconds
per DOI, depending mostly on how fast DataCite is responding.

If a lookup fails (network trouble, a server error), the other DOIs still
complete, and the failures are listed in `df.attrs["failed_dois"]`.

To query only some databases: `find_citations(dois, sources=["openalex"])`.

## Large lists, and refreshing them periodically

For thousands of DOIs, or a list you'll re-check every month, pass a SQLite
file as `db_path`. Each DOI's results are saved as soon as they arrive, and
the returned table includes everything found for your DOIs, in this run or
earlier ones.

```python
from datetime import timedelta
from dataset_citation_finder import find_citations

df = find_citations(
    dois,
    db_path="citations.sqlite",
    refresh_after=timedelta(days=30),
    email="you@example.org",
)
```

`refresh_after=timedelta(days=30)` means: skip any DOI that a database
already answered in the last 30 days. Running this same call:

- **for the first time** queries every DOI;
- **after an interruption** (Ctrl-C, a crash, a closed laptop) picks up where
  it stopped: finished DOIs are skipped;
- **a month later** re-checks every DOI and adds any new citations;
- **with new DOIs added to the list** queries only the new ones, until the
  30 days are up.

DOIs that failed are never counted as answered, so they're retried on the
next run.

To see what's new since a given date:

```python
new = df[df["first_seen"] >= "2026-10-01"]
```

To load the saved results without querying anything:

```python
from dataset_citation_finder import read_citations

df = read_citations("citations.sqlite")             # one row per citation
raw = read_citations("citations.sqlite", raw=True)  # one row per source, with first_seen/last_seen
```

Without `refresh_after`, a run with `db_path` re-queries every DOI. That's
useful when you want a full refresh now.

The store keeps two tables: `citations` (one row per source's report of a
citation, with `first_seen` and `last_seen` timestamps) and `queried` (when
each DOI was last answered by each database). Any SQLite tool can open it.

See `docs/user_guide.md` for tips on very large runs (whole repositories,
multi-day runs, OpenAlex's daily limit).

## Every DOI under a prefix

```python
from pathlib import Path
from dataset_citation_finder import dois_under_prefix

dois = dois_under_prefix("10.6073", email="you@example.org")  # every EDI DOI
Path("edi_dois.txt").write_text("\n".join(dois))
```

Listing a big prefix takes a while (about a minute per ~10,000 DOIs), so
save the list and reuse it. Then pass it to `find_citations` with a
`db_path` as above.

Every EDI DOI looks like `10.6073/pasta/<hash>`, so a single EDI scope can't
be selected by prefix. `demo/fetch_scope_dois.py` lists one scope's DOIs:

```bash
uv run demo/fetch_scope_dois.py knb-lter-nes   # writes knb-lter-nes_dois.txt
```

## API keys (optional, recommended for OpenAlex)

OpenAlex limits how many requests you can make per day; a free key from
<https://openalex.org/settings/api> raises the limit. OpenCitations also
offers free tokens. Pass keys directly or set environment variables:

```python
find_citations(dois, api_keys={"openalex": "...", "opencitations": "..."})
```

```bash
export OPENALEX_API_KEY=...
export OPENCITATIONS_API_KEY=...
```

If OpenAlex's daily limit runs out mid-run, OpenAlex stops (the other
databases carry on) and its unfinished DOIs are reported as failed. With a
`db_path` and `refresh_after`, re-run the next day to continue.

## Check your DOIs first (optional)

```python
from dataset_citation_finder import validate_dois

check = validate_dois(dois)
check[check["status"] != "registered"]   # typos, unregistered DOIs
check[check["registrar"] == "crossref"]  # usually paper DOIs pasted in by mistake
```

## Run the tests

```bash
uv run pytest
```
