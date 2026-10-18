"""HTTP helpers: a polite client and a GET that retries transient failures."""

from __future__ import annotations

import logging
import random
import time

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
USER_AGENT = "dataset-citation-finder/0.4 (https://github.com/example/dataset-citation-finder)"

MAX_ATTEMPTS = 5
# Some APIs (notably OpenAlex after throttling an IP) send Retry-After values
# of many hours. Past this cap we stop waiting and hand the response back.
MAX_RETRY_AFTER = 120.0
RETRY_STATUSES = (429, 500, 502, 503, 504)


def make_client(email: str | None = None) -> httpx.Client:
    """Build a client whose User-Agent carries a contact email if given.

    DataCite, OpenAlex and CrossRef ask for a `mailto:` so they can contact
    a misbehaving client instead of blocking it.
    """
    ua = f"{USER_AGENT} (mailto:{email})" if email else USER_AGENT
    return httpx.Client(
        timeout=TIMEOUT,
        headers={"User-Agent": ua, "Accept": "application/json"},
        follow_redirects=True,
    )


def get(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    retry_statuses: tuple[int, ...] = RETRY_STATUSES,
    max_attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """GET `url`, retrying network errors and `retry_statuses` with backoff.

    Returns the last response even if it is still an error, so the caller
    decides what an error means for that source. Network errors on the final
    attempt are raised.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as e:
            if attempt == max_attempts:
                raise
            wait = _backoff(attempt)
            log.warning("network error on %s (attempt %d): %s; retrying in %.1fs",
                        url, attempt, e, wait)
            time.sleep(wait)
            continue

        if resp.status_code not in retry_statuses or attempt == max_attempts:
            return resp

        retry_after = _retry_after_seconds(resp)
        if retry_after is not None and retry_after > MAX_RETRY_AFTER:
            log.warning("HTTP %d on %s; server asked us to wait %.0fs, more than "
                        "the %.0fs cap — giving up", resp.status_code, url,
                        retry_after, MAX_RETRY_AFTER)
            return resp
        wait = retry_after if retry_after is not None else _backoff(attempt)
        log.warning("HTTP %d on %s (attempt %d); retrying in %.1fs",
                    resp.status_code, url, attempt, wait)
        time.sleep(wait)

    raise AssertionError("unreachable")


def _backoff(attempt: int) -> float:
    """Exponential backoff (1s, 2s, 4s, ... capped at 30s) with jitter."""
    return min(2 ** (attempt - 1), 30.0) * random.uniform(0.5, 1.0)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None  # HTTP-date form; fall back to our own backoff
