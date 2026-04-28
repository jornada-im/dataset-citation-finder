"""Resolution and storage of per-source API keys.

API keys can be supplied two ways, in order of precedence:

1. **Explicit `api_keys=` parameter** on `find_citations*` / `validate_dois`.
   A dict mapping source-id → key:

       api_keys = {"openalex": "...", "semantic_scholar": "..."}

2. **Environment variables.** The library checks `<SOURCE>_API_KEY` for each
   adapter. For example, `OPENALEX_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY`.

When both are present for a given source, the explicit dict wins. Sources
not listed are queried unauthenticated (polite-pool only).

Keys are passed to adapter constructors via the `api_key` keyword argument.
Adapters that don't accept one are unaffected.
"""

from __future__ import annotations

import os

# Adapter source-id → environment variable name. Add a row here when an
# adapter gains API-key support.
_ENV_VAR_FOR_SOURCE: dict[str, str] = {
    "openalex": "OPENALEX_API_KEY",
    "opencitations": "OPENCITATIONS_API_KEY",
    "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY",
    "dimensions": "DIMENSIONS_API_KEY",
}


def resolve_api_keys(
    explicit: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge an explicit api_keys dict with environment-variable fallbacks.

    Returns a dict containing only sources for which a key was found from
    either source. Empty/blank values are dropped.
    """
    explicit = explicit or {}
    out: dict[str, str] = {}
    for sid, env_var in _ENV_VAR_FOR_SOURCE.items():
        if sid in explicit and explicit[sid]:
            out[sid] = explicit[sid]
            continue
        env_value = os.environ.get(env_var)
        if env_value:
            out[sid] = env_value
    # Allow keys for sources we don't know about yet (forward compatible).
    for sid, key in explicit.items():
        if sid not in out and key:
            out[sid] = key
    return out
