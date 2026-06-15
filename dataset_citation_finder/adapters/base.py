"""Base adapter interface.

Every source adapter implements `SourceAdapter`. The engine dispatches to
the most efficient query method an adapter exposes:

- `query_prefix(prefix)` when available (DataCite Events)
- `query_batch(dois)` when available (Dimensions)
- `query_single(doi)` always available; default `query_batch` impl fans out
  over this with bounded concurrency.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import AsyncIterator, Iterable

import httpx

from ..schema import LongRow

log = logging.getLogger(__name__)


class SourceAdapter(abc.ABC):
    """Abstract base for all source adapters."""

    source_id: str  # e.g. "datacite_events"; set on each subclass
    supports_prefix: bool = False
    supports_batch: bool = False
    # Adapters with strict rate limits can lower this; the engine respects it
    # as a ceiling even when the caller requests higher global concurrency.
    default_concurrency: int = 8
    # Minimum pause held inside the semaphore after each request completes.
    # Prevents bursty patterns when responses are fast.
    inter_request_delay: float = 0.0

    def __init__(self, client: httpx.AsyncClient, *, concurrency: int = 8) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        # DOIs whose per-DOI query failed (transport error, exhausted retries,
        # or HTTP >= 400 other than 404). The engine reads + clears this after
        # each query_batch call so failed DOIs aren't marked "queried" — they
        # need to be re-tried on the next run.
        self._failed_dois: set[str] = set()

    def _mark_failed(self, doi: str) -> None:
        self._failed_dois.add(doi)

    def take_failed_dois(self) -> set[str]:
        """Return and clear the set of DOIs that failed since the last call."""
        out = self._failed_dois
        self._failed_dois = set()
        return out

    @abc.abstractmethod
    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        """Yield LongRow instances for citations of a single DOI."""
        raise NotImplementedError
        # The `yield` below is needed to make this an async generator at type
        # level; subclasses override.
        yield  # type: ignore[unreachable]

    async def query_batch(self, dois: Iterable[str]) -> AsyncIterator[LongRow]:
        """Yield LongRow instances for a batch of DOIs.

        Default implementation: concurrent per-DOI calls bounded by the
        adapter's semaphore. Adapters with native batching override this.
        """
        dois = list(dois)

        async def _run_one(doi: str) -> list[LongRow]:
            async with self._semaphore:
                rows: list[LongRow] = []
                try:
                    async for row in self.query_single(doi):
                        rows.append(row)
                except Exception:
                    log.exception("%s: query_single(%s) failed", self.source_id, doi)
                    self._mark_failed(doi)
                if self.inter_request_delay > 0:
                    await asyncio.sleep(self.inter_request_delay)
                return rows

        tasks = [asyncio.create_task(_run_one(d)) for d in dois]
        for task in asyncio.as_completed(tasks):
            for row in await task:
                yield row

    async def query_prefix(self, prefix: str) -> AsyncIterator[LongRow]:
        """Yield LongRow instances for all citations of DOIs under `prefix`.

        Only meaningful for adapters with `supports_prefix = True`.
        """
        raise NotImplementedError(
            f"{self.source_id} does not support prefix queries; "
            "enumerate DOIs and call query_batch instead."
        )
        yield  # type: ignore[unreachable]
