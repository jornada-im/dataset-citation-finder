"""SQLite store for dataset citation results.

Public surface:
    - open_store(db_path, ...) -> sqlite3.Connection
    - describe_store(db_path) -> StoreInfo
    - StoreFormatError, SchemaVersionError, MigrationRequired
    - CURRENT_SCHEMA_VERSION
"""

from __future__ import annotations

from .connection import describe_store, open_store, StoreInfo
from .errors import MigrationRequired, SchemaVersionError, StoreFormatError
from .schema import APPLICATION_ID, CURRENT_SCHEMA_VERSION

__all__ = [
    "open_store",
    "describe_store",
    "StoreInfo",
    "StoreFormatError",
    "SchemaVersionError",
    "MigrationRequired",
    "CURRENT_SCHEMA_VERSION",
    "APPLICATION_ID",
]
