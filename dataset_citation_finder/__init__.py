"""Dataset Citation Finder — discover papers citing a dataset across multiple
scholarly databases."""

__version__ = "0.3.0"

from .api import (
    RunSummary,
    coalesce_citations,
    collapse_citations_df,
    describe_store,
    export_citations,
    find_citations,
    find_citations_batch,
    find_citations_by_prefix,
    open_store,
    read_citations,
    validate_dois,
)
from .schema import GroupedRecord, LongRow
from .store import (
    CURRENT_SCHEMA_VERSION,
    MigrationRequired,
    SchemaVersionError,
    StoreFormatError,
    StoreInfo,
)

__all__ = [
    "find_citations",
    "find_citations_batch",
    "find_citations_by_prefix",
    "validate_dois",
    "coalesce_citations",
    "collapse_citations_df",
    "export_citations",
    "read_citations",
    "open_store",
    "describe_store",
    "LongRow",
    "GroupedRecord",
    "RunSummary",
    "StoreInfo",
    "StoreFormatError",
    "SchemaVersionError",
    "MigrationRequired",
    "CURRENT_SCHEMA_VERSION",
    "__version__",
]
