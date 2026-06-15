"""Adapter registry.

Maps source IDs to adapter classes. The engine uses this to instantiate
the requested adapters.
"""

from __future__ import annotations

from .base import SourceAdapter
from .datacite_events import DataCiteEventsAdapter
from .opencitations import OpenCitationsAdapter
from .openalex import OpenAlexAdapter
from .stubs import (
    CrossRefAdapter,
    DimensionsAdapter,
    GoogleScholarAdapter,
    SemanticScholarAdapter,
)

# Default adapter set (see design §4.1): only sources that are actually
# implemented. The stubs (semantic_scholar, crossref) stay in REGISTRY so they
# can be opted into explicitly, but they're not run by default — they'd only
# raise NotImplementedError and pollute every run's errors_per_source. Add a
# stub back here when it's built. Dimensions is auto-added when credentials are
# detected (handled in api.py); Google Scholar is opt-in only.
DEFAULT_SOURCES: tuple[str, ...] = (
    "datacite_events",
    "openalex",
    "opencitations",
)

REGISTRY: dict[str, type[SourceAdapter]] = {
    "datacite_events": DataCiteEventsAdapter,
    "opencitations": OpenCitationsAdapter,
    "openalex": OpenAlexAdapter,
    "semantic_scholar": SemanticScholarAdapter,
    "crossref": CrossRefAdapter,
    "dimensions": DimensionsAdapter,
    "google_scholar": GoogleScholarAdapter,
}

__all__ = [
    "SourceAdapter",
    "DataCiteEventsAdapter",
    "OpenCitationsAdapter",
    "OpenAlexAdapter",
    "SemanticScholarAdapter",
    "CrossRefAdapter",
    "DimensionsAdapter",
    "GoogleScholarAdapter",
    "REGISTRY",
    "DEFAULT_SOURCES",
]
