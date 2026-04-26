"""Tests for the default SourceAdapter.query_batch fanout behavior."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from dataset_citation_finder.adapters.base import SourceAdapter
from dataset_citation_finder.schema import LongRow


class TrackingAdapter(SourceAdapter):
    """Adapter that records which DOIs query_single was called with."""

    source_id = "tracking"

    def __init__(self, *, concurrency: int = 8, sleep: float = 0):
        # Don't call super() — no HTTP client needed.
        self.supports_prefix = False
        self.supports_batch = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self.called_for: list[str] = []
        self.concurrent_peak = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def query_single(self, doi: str) -> AsyncIterator[LongRow]:
        async with self._lock:
            self._in_flight += 1
            self.concurrent_peak = max(self.concurrent_peak, self._in_flight)
        try:
            if self._sleep:
                await asyncio.sleep(self._sleep)
            self.called_for.append(doi)
            yield LongRow(
                cited_doi=doi,
                citing_doi=f"10.1234/citer-of-{doi.split('/')[-1]}",
                source=self.source_id,
            )
        finally:
            async with self._lock:
                self._in_flight -= 1


class TestQueryBatchFanout:

    @pytest.mark.asyncio
    async def test_calls_query_single_for_each_doi(self):
        adapter = TrackingAdapter()
        rows = [r async for r in adapter.query_batch(
            ["10.6073/a", "10.6073/b", "10.6073/c"]
        )]
        assert len(rows) == 3
        assert sorted(adapter.called_for) == ["10.6073/a", "10.6073/b", "10.6073/c"]

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded(self):
        """With concurrency=2 and slow single queries, peak parallelism should be 2."""
        adapter = TrackingAdapter(concurrency=2, sleep=0.05)
        dois = [f"10.6073/{i}" for i in range(8)]
        rows = [r async for r in adapter.query_batch(dois)]
        assert len(rows) == 8
        assert adapter.concurrent_peak <= 2

    @pytest.mark.asyncio
    async def test_empty_batch_yields_nothing(self):
        adapter = TrackingAdapter()
        rows = [r async for r in adapter.query_batch([])]
        assert rows == []
        assert adapter.called_for == []

    @pytest.mark.asyncio
    async def test_inter_request_delay_is_honored(self):
        """inter_request_delay holds the semaphore slot, spreading requests out."""
        delay = 0.05
        adapter = TrackingAdapter(concurrency=1)
        adapter.inter_request_delay = delay
        dois = ["10.6073/a", "10.6073/b", "10.6073/c"]
        t0 = time.monotonic()
        rows = [r async for r in adapter.query_batch(dois)]
        elapsed = time.monotonic() - t0
        assert len(rows) == 3
        # With concurrency=1, each slot must be held for at least `delay`
        # seconds, so total time >= n * delay.
        assert elapsed >= len(dois) * delay
