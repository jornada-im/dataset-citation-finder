"""Download every DOI DataCite holds for one EDI scope.

The package's prefix sweep works on DOI prefixes, and every EDI DOI is
``10.6073/pasta/<hash>`` — so a scope (e.g. ``knb-lter-nes``) can't be
selected by prefix. DataCite does, however, store each record's PASTA URL
(``.../mapbrowse?packageid=<scope>.<id>.<rev>``), which we can filter on.

Every package revision has its own DOI, so the count is per-revision.

Usage:
    python demo/fetch_scope_dois.py knb-lter-nes
    python demo/fetch_scope_dois.py knb-lter-nes -o nes_dois.txt
"""

from __future__ import annotations

import argparse

import httpx

DOIS_URL = "https://api.datacite.org/dois"
EDI_PREFIX = "10.6073"


def fetch_scope_dois(scope: str) -> list[str]:
    params: dict | None = {
        "prefix": EDI_PREFIX,
        "query": f"identifiers.identifier:*{scope}*",
        "fields[dois]": "doi,url",
        "page[size]": 1000,
        "page[cursor]": 1,
    }
    url: str | None = DOIS_URL
    dois: set[str] = set()
    with httpx.Client(timeout=60) as client:
        while url:
            r = client.get(url, params=params)
            r.raise_for_status()
            body = r.json()
            for rec in body["data"]:
                landing = rec["attributes"].get("url") or ""
                # Exact scope match: "knb-lter-nes." must not match "knb-lter-nesx."
                if f"packageid={scope}." in landing:
                    dois.add(rec["attributes"]["doi"].lower())
            # The `next` link already carries the query string.
            url, params = body.get("links", {}).get("next"), None
    return sorted(dois)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scope", help="EDI scope, e.g. knb-lter-nes")
    parser.add_argument("-o", "--output", help="output file (default: <scope>_dois.txt)")
    args = parser.parse_args()

    dois = fetch_scope_dois(args.scope)
    out = args.output or f"{args.scope}_dois.txt"
    with open(out, "w") as f:
        f.write("\n".join(dois) + "\n")
    print(f"{len(dois)} DOIs written to {out}")


if __name__ == "__main__":
    main()
