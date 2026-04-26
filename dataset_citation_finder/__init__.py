"""Dataset Citation Finder — discover papers citing a dataset across multiple
scholarly databases."""

from .api import (
    collapse_sources,
    find_citations,
    find_citations_batch,
    find_citations_by_shoulder,
)
from .schema import GroupedRecord, LongRow

__version__ = "0.1.0"

__all__ = [
    "find_citations",
    "find_citations_batch",
    "find_citations_by_shoulder",
    "collapse_sources",
    "LongRow",
    "GroupedRecord",
    "__version__",
]
