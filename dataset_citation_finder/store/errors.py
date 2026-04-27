"""Exceptions raised by the SQLite store layer."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for store-layer errors."""


class StoreFormatError(StoreError):
    """The file exists but is not a recognizable dataset-citation-finder store.

    Raised when SQLite's `application_id` PRAGMA does not match the value this
    library writes, or the `meta` table is absent.
    """


class SchemaVersionError(StoreError):
    """The store was written by a newer library version than this one understands."""


class MigrationRequired(StoreError):
    """The store's schema is older than current and `migrate=False` was passed."""
