# Dataset Citation Finder

A Python library for discovering academic papers that cite a given dataset,
across multiple scholarly databases and citation graphs.

See `docs/dataset_citation_finder_design.md` for the full design document.

## Status

Early development. Implemented and tested:

- `utils.normalize_doi` — canonical DOI form
- `schema.LongRow` / `schema.GroupedRecord` — pydantic models
- Adapters (fully working): **DataCite Event Data**, **OpenCitations**, **OpenAlex**
- Adapters (stubs, raise `NotImplementedError`): Semantic Scholar, CrossRef, Dimensions, Google Scholar
- `engine.collect_rows` — adapter orchestration with per-adapter isolation
- `collapse.collapse_sources` — long-format → deduplicated view
- `enumeration.PrefixEnumerator` — DataCite-backed DOI enumeration with disk cache
- `http_client` — polite-pool User-Agent, exponential backoff with jitter, `Retry-After` respect
- Public API: `find_citations`, `find_citations_batch`, `find_citations_by_prefix`, `collapse_sources`

Not yet implemented:

- Remaining adapters (Semantic Scholar, CrossRef, Dimensions, Google Scholar)
- Checkpoint / resume store (SQLite)
- BibTeX and CSV file-output formatters (the long-format DataFrame is already usable via `.to_csv` / `.to_parquet`)
- End-to-end integration tests against live services

## Install

```bash
pip install -e .
```

Optional extras:

```bash
pip install -e ".[parquet]"    # pyarrow for Parquet output
pip install -e ".[bibtex]"     # bibtexparser (once BibTeX formatter is added)
pip install -e ".[progress]"   # tqdm progress bars
pip install -e ".[dev]"        # pytest, pytest-asyncio
```

## Usage

```python
from dataset_citation_finder import (
    find_citations,
    find_citations_batch,
    find_citations_by_prefix,
    collapse_sources,
)

# Single DOI
df = find_citations("10.6073/pasta/abc123", email="you@example.org")

# Arbitrary list of DOIs
df = find_citations_batch([
    "10.6073/pasta/abc",
    "10.5281/zenodo.1234567",
])

# Full repository sweep by DOI prefix
df = find_citations_by_prefix("10.6073", email="you@example.org",
                                cache_dir="~/.cache/dcf")

# Collapse to one row per unique edge with sources as a list
collapsed = collapse_sources(df)
```

Default output is a long-format pandas DataFrame with columns:
`cited_doi`, `citing_doi`, `source`, `relation_type`, `retrieved_at`.
Pass `output="grouped"` to get a list of `GroupedRecord` objects instead.

## Run the tests

```bash
python -m pytest
```

Current coverage: **81 tests across 9 files**, all green.
