"""Stub adapters for sources deferred to a later implementation pass.

Each class is a placeholder that implements the SourceAdapter interface
but raises `NotImplementedError` if invoked. They exist so the adapter
registry has a complete picture of what's planned and so test scaffolding
can reference them by name.

TODO: full implementation of each adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..schema import LongRow
from .base import SourceAdapter


class _Stub(SourceAdapter):
    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:  # pragma: no cover
        raise NotImplementedError(
            f"{self.source_id} adapter is not yet implemented. "
            "See design doc §15 implementation plan."
        )
        yield  # type: ignore[unreachable]


class SemanticScholarAdapter(_Stub):
    source_id = "semantic_scholar"
    supports_prefix = False
    supports_batch = True  # the API has /graph/v1/paper/batch


class CrossRefAdapter(_Stub):
    source_id = "crossref"
    supports_prefix = False
    supports_batch = False


class DimensionsAdapter(_Stub):
    source_id = "dimensions"
    supports_prefix = False
    supports_batch = True  # native list-valued DSL filter


class GoogleScholarAdapter(_Stub):
    source_id = "google_scholar"
    supports_prefix = False
    supports_batch = False
