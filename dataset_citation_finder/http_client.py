"""Shared HTTP client factory.

Provides a configured `httpx.AsyncClient` with polite-pool headers,
a per-adapter semaphore for concurrency control, and exponential-backoff
retry on 429/503.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
DEFAULT_USER_AGENT = "dataset-citation-finder/0.1 (https://github.com/example/dataset-citation-finder)"


@dataclass
class RetryPolicy:
    """Exponential backoff with jitter for retryable HTTP errors."""

    max_attempts: int = 5
    base_delay: float = 1.0  # seconds
    max_delay: float = 30.0
    # Hard ceiling on how long we'll honor a server-supplied Retry-After.
    # Some APIs (notably OpenAlex when an IP has been throttled) return
    # multi-hour values that would stall a sweep indefinitely. When the
    # server asks for more than this we surface the response to the caller
    # instead of sleeping.
    max_retry_after: float = 120.0
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504)

    def delay_for(self, attempt: int) -> float:
        """Return seconds to wait before attempt `attempt` (1-indexed)."""
        exp = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return exp * (0.5 + random.random() / 2)  # [0.5x, 1.0x] jitter


def make_client(*, email: str | None = None, user_agent: str | None = None) -> httpx.AsyncClient:
    """Build an AsyncClient with polite headers set.

    Several services (CrossRef, DataCite, OpenAlex) ask clients to include
    a contact email in the User-Agent so they can reach out about misbehaving
    clients rather than just blocking them.
    """
    ua = user_agent or DEFAULT_USER_AGENT
    if email:
        ua = f"{ua} (mailto:{email})"
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": ua, "Accept": "application/json"},
        follow_redirects=True,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry: RetryPolicy | None = None,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying on transient errors."""
    retry = retry or RetryPolicy()
    last_exc: Exception | None = None

    for attempt in range(1, retry.max_attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt == retry.max_attempts:
                raise
            delay = retry.delay_for(attempt)
            log.warning("transport error on %s %s (attempt %d): %s; retrying in %.1fs",
                        method, url, attempt, e, delay)
            await asyncio.sleep(delay)
            continue

        if resp.status_code not in retry.retry_on_status:
            return resp

        if attempt == retry.max_attempts:
            return resp  # surface the final response to the caller

        # Honor Retry-After when present, but cap it. A server asking us to
        # sleep for hours (OpenAlex IP cooldowns reach ~19h) would stall the
        # whole pipeline; in that case we give up and surface the 429 so the
        # caller can decide what to do.
        ra = resp.headers.get("Retry-After")
        ra_seconds = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
        if ra_seconds is not None and ra_seconds > retry.max_retry_after:
            log.warning(
                "status %d on %s %s (attempt %d); server requested "
                "Retry-After=%.0fs which exceeds cap=%.0fs — giving up",
                resp.status_code, method, url, attempt,
                ra_seconds, retry.max_retry_after,
            )
            return resp
        delay = ra_seconds if ra_seconds is not None else retry.delay_for(attempt)
        log.warning("status %d on %s %s (attempt %d); retrying in %.1fs",
                    resp.status_code, method, url, attempt, delay)
        await asyncio.sleep(delay)

    # Unreachable under normal control flow.
    raise RuntimeError(f"request_with_retry exhausted without result: {last_exc}")
