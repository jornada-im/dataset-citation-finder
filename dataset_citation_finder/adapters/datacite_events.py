"""DataCite Event Data adapter.

Queries https://api.datacite.org/events for citation relationships involving
dataset DOIs. Supports both single-DOI and prefix queries.

Relation-type handling: DataCite records citations as six types in three
inverse pairs — see design doc §8.1. This adapter treats all six as citations
by default and normalizes direction so `cited_doi` is always the dataset.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any

from ..http_client import request_with_retry
from ..schema import LongRow
from ..utils import normalize_doi
from .base import SourceAdapter

log = logging.getLogger(__name__)

EVENTS_URL = "https://api.datacite.org/events"

# DataCite Events read as "subj <relation-type-id> obj":
#   "subj is-cited-by obj"  → subj is the cited entity (dataset),
#                              obj is the citer (paper).
#   "subj cites obj"        → subj is the citer (paper),
#                              obj is the cited entity (dataset).
#
# These constants classify each relation type by which side of the event the
# dataset (the cited entity) appears on.

# Dataset is on the SUBJECT side: subj-id = dataset, obj-id = citer.
SUBJECT_SIDE_RELATIONS = frozenset({
    "is-cited-by",
    "is-referenced-by",
    "is-supplement-to",
})

# Dataset is on the OBJECT side: obj-id = dataset, subj-id = citer. CrossRef-
# derived events in DataCite Event Data use `references`, so dropping these
# would miss the bulk of real citations.
OBJECT_SIDE_RELATIONS = frozenset({
    "cites",
    "references",
    "is-supplemented-by",
})

DEFAULT_RELATION_TYPES: tuple[str, ...] = tuple(
    sorted(OBJECT_SIDE_RELATIONS | SUBJECT_SIDE_RELATIONS)
)


class DataCiteEventsAdapter(SourceAdapter):
    source_id = "datacite_events"
    supports_prefix = True
    supports_batch = False  # default per-DOI concurrent batching is fine

    def __init__(
        self,
        client,
        *,
        concurrency: int = 8,
        relation_types: Iterable[str] = DEFAULT_RELATION_TYPES,
        page_size: int = 1000,
    ) -> None:
        super().__init__(client, concurrency=concurrency)
        self._relation_types = tuple(relation_types)
        self._page_size = page_size

    # ---- public query methods -------------------------------------------

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        """Query events where the given DOI is one of the participants.

        Two paginated streams: one filters `obj-id=<doi>` for OBJECT-side
        relations (where the dataset is the object: `cites`, `references`,
        `is-supplemented-by`); the other filters `subj-id=<doi>` for
        SUBJECT-side relations (`is-cited-by`, `is-referenced-by`,
        `is-supplement-to`). Both yield `LongRow`s with `cited_doi=<doi>`
        regardless of which side the event recorded the dataset on.
        """
        # Dataset is on obj-id: relations cites / references / is-supplemented-by.
        async for event in self._paginate({
            "obj-id": doi,
            "relation-type-id": self._object_side_filter(),
        }):
            row = self._event_to_row(event, side="object")
            if row is not None:
                yield row

        # Dataset is on subj-id: relations is-cited-by / is-referenced-by / is-supplement-to.
        async for event in self._paginate({
            "subj-id": doi,
            "relation-type-id": self._subject_side_filter(),
        }):
            row = self._event_to_row(event, side="subject")
            if row is not None:
                yield row

    async def query_prefix(self, prefix: str) -> AsyncIterator[LongRow]:
        """Stream all citation events for DOIs matching `<prefix>/*`.

        DataCite Events supports a top-level `prefix=<prefix>` filter (NOT
        `obj-id.prefix=...`, which is silently ignored). `prefix=` matches
        whichever side of the event the prefix appears on, so we issue two
        queries narrowed by relation type — one for object-side relations
        (dataset on obj-id) and one for subject-side relations (dataset on
        subj-id) — and then post-filter to keep only events where the
        dataset DOI actually starts with `<prefix>` (since `prefix=` would
        also match if a non-dataset participant happened to share the
        registrant prefix).
        """
        # Object-side relations: dataset is on obj-id.
        async for event in self._paginate({
            "prefix": prefix,
            "relation-type-id": self._object_side_filter(),
        }):
            row = self._event_to_row(event, side="object")
            if row is not None and row.cited_doi.startswith(prefix):
                yield row

        # Subject-side relations: dataset is on subj-id.
        async for event in self._paginate({
            "prefix": prefix,
            "relation-type-id": self._subject_side_filter(),
        }):
            row = self._event_to_row(event, side="subject")
            if row is not None and row.cited_doi.startswith(prefix):
                yield row

    # ---- internals ------------------------------------------------------

    def _object_side_filter(self) -> str:
        return ",".join(r for r in self._relation_types if r in OBJECT_SIDE_RELATIONS)

    def _subject_side_filter(self) -> str:
        return ",".join(r for r in self._relation_types if r in SUBJECT_SIDE_RELATIONS)

    async def _paginate(self, params: dict[str, str | int]) -> AsyncIterator[dict]:
        """Yield individual event dicts across all pages of a query."""
        # DataCite's cursor pagination: include page[size], start with
        # page[cursor]=1, follow the `next` link thereafter.
        cursor: str | None = "1"
        # Skip relation-type filter if empty (e.g. caller passed relation_types=[]).
        effective_params: dict[str, str | int] = {
            k: v for k, v in params.items() if not (k == "relation-type-id" and not v)
        }
        effective_params["page[size]"] = self._page_size

        while cursor is not None:
            effective_params["page[cursor]"] = cursor
            resp = await request_with_retry(self._client, "GET", EVENTS_URL, params=effective_params)
            if resp.status_code >= 400:
                log.warning("datacite_events: HTTP %d for params=%s", resp.status_code, effective_params)
                return
            body = resp.json()
            for event in body.get("data", []):
                yield event
            cursor = _next_cursor(body)

    def _event_to_row(
        self,
        event: dict[str, Any],
        *,
        side: str,
    ) -> LongRow | None:
        """Build a LongRow from a raw DataCite event dict.

        `side` indicates which endpoint of the event the dataset (cited
        entity) appears on, derived from the relation-type filter applied
        when the query was issued. The mapping must agree with the
        OBJECT_SIDE_RELATIONS / SUBJECT_SIDE_RELATIONS classification.
        """
        attrs = event.get("attributes", {}) or {}
        relation = attrs.get("relation-type-id")
        if relation not in self._relation_types:
            return None

        obj_id = _extract_doi(attrs.get("obj-id"))
        subj_id = _extract_doi(attrs.get("subj-id"))

        # Normalize direction so cited_doi is always the dataset.
        if side == "object":
            cited = obj_id
            citing = subj_id
        else:  # side == "subject"
            cited = subj_id
            citing = obj_id

        if cited is None or citing is None:
            return None

        # Only count dataset<->scholarly-work edges; skip DOI<->URL and similar.
        try:
            return LongRow(
                cited_doi=cited,
                citing_doi=citing,
                source=self.source_id,
                relation_type=relation,
                extra={
                    "event_id": event.get("id"),
                    "upstream_source": attrs.get("source-id"),
                    "occurred_at": attrs.get("occurred-at"),
                },
            )
        except ValueError:
            # DOI normalization rejected one of the IDs (e.g. it was a URL).
            return None


# ---- helpers ------------------------------------------------------------

def _extract_doi(val: Any) -> str | None:
    """DataCite event ids may be bare DOIs or doi.org URLs. Normalize."""
    if not isinstance(val, str):
        return None
    return normalize_doi(val)


def _next_cursor(body: dict) -> str | None:
    """Extract the next-page cursor from a DataCite JSON:API response."""
    links = body.get("links") or {}
    next_url = links.get("next")
    if not next_url:
        return None
    # The `next` link embeds page[cursor]=...; parse it out.
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(next_url).query)
    cursors = qs.get("page[cursor]")
    if not cursors:
        return None
    return cursors[0]
