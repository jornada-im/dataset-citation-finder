"""Dataset Citation Finder — find papers that cite a dataset, using several
scholarly databases."""

__version__ = "0.4.0"

from .api import dois_under_prefix, find_citations, read_citations
from .store import StoreError
from .utils import normalize_doi
from .validation import validate_dois

__all__ = [
    "find_citations",
    "read_citations",
    "dois_under_prefix",
    "validate_dois",
    "normalize_doi",
    "StoreError",
    "__version__",
]
