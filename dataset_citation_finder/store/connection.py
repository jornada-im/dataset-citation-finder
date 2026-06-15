"""open_store(): the single entry point for getting a connection to the store.

Validates that an existing file is one of ours (via PRAGMA application_id +
the `meta` table), runs any pending migrations, and stamps `meta.library_version`
on every successful open. See design doc §7.4.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..utils import utcnow_iso
from .errors import MigrationRequired, SchemaVersionError, StoreFormatError
from .migrations import discover_migrations
from .schema import APPLICATION_ID, CURRENT_SCHEMA_VERSION, SOURCE_TABLES

log = logging.getLogger(__name__)


def _library_version() -> str:
    # Imported lazily to avoid a circular import at module load.
    from .. import __version__
    return __version__


def open_store(
    db_path: str | Path,
    *,
    create: bool = True,
    migrate: bool = True,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite store, validating and migrating as needed.

    Parameters
    ----------
    db_path : str | Path
        Path to a SQLite file. ":memory:" is allowed for tests.
    create : bool
        If True (default), create the file if it does not exist.
    migrate : bool
        If True (default), run any pending migrations before returning.
        If False and the schema is older than current, raises MigrationRequired.
    read_only : bool
        Open the file in read-only mode (uses URI mode=ro). The connection
        will refuse writes; useful for describe_store / read_citations.

    Raises
    ------
    StoreFormatError
        The file exists but is not a recognizable dataset-citation-finder store.
    SchemaVersionError
        The file has a schema_version greater than this library understands.
    MigrationRequired
        The file's schema is older than current and `migrate=False`.
    FileNotFoundError
        The file does not exist and `create=False`.
    """
    path_str = str(db_path)
    in_memory = path_str == ":memory:"
    if not in_memory:
        path = Path(db_path).expanduser()
        path_str = str(path)
        if not path.exists():
            if not create:
                raise FileNotFoundError(f"store does not exist: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)

    if read_only and not in_memory:
        # URI form to request read-only opening.
        uri = f"file:{path_str}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(path_str, isolation_level=None)

    conn.row_factory = sqlite3.Row

    # PRAGMA settings: WAL for concurrent readers; foreign keys on.
    if not read_only:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # In-memory DBs and some filesystems can't WAL; not fatal.
            pass
    conn.execute("PRAGMA foreign_keys=ON")

    is_new = _is_new_database(conn)

    if is_new:
        if not create:
            # We opened an empty file but the caller said no-create. Treat as
            # missing.
            conn.close()
            raise FileNotFoundError(f"store does not exist or is empty: {db_path}")
        _initialize_new_store(conn)
    else:
        _validate_existing_store(conn, migrate=migrate)

    # Stamp the current library version on every successful open (writers only).
    if not read_only:
        conn.execute(
            "UPDATE meta SET library_version = ?",
            (_library_version(),),
        )

    return conn


def _is_new_database(conn: sqlite3.Connection) -> bool:
    """A 'new' DB has no application_id and no `meta` table."""
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    meta_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone() is not None
    return app_id == 0 and not meta_exists


def _initialize_new_store(conn: sqlite3.Connection) -> None:
    """Stamp the file as ours and run all migrations from scratch."""
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")

    migrations = discover_migrations()
    if not migrations or migrations[0].from_version != 0:
        raise RuntimeError("no initial migration (from_version=0) found")

    conn.execute("BEGIN")
    try:
        # Apply v0 -> v1 (creates meta and the rest of the initial schema).
        first = migrations[0]
        first.apply(conn)

        # Insert meta with the version reached so far; updated below.
        version = _library_version()
        now = utcnow_iso()
        conn.execute(
            "INSERT INTO meta (schema_version, library_version, created_at, "
            "created_by, application_id) VALUES (?, ?, ?, ?, ?)",
            (first.to_version, version, now, version, APPLICATION_ID),
        )
        current = first.to_version

        # Apply any further migrations to reach CURRENT_SCHEMA_VERSION.
        for m in migrations[1:]:
            if m.from_version != current:
                raise RuntimeError(
                    f"migration gap during init: at v{current} but next is v{m.from_version}"
                )
            m.apply(conn)
            conn.execute("UPDATE meta SET schema_version = ?", (m.to_version,))
            current = m.to_version

        conn.execute(f"PRAGMA user_version = {current}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _validate_existing_store(conn: sqlite3.Connection, *, migrate: bool) -> None:
    """Verify application_id and meta, then migrate if needed."""
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    if app_id != 0 and app_id != APPLICATION_ID:
        raise StoreFormatError(
            f"file has application_id={app_id:#x}; "
            f"expected {APPLICATION_ID:#x} (dataset-citation-finder)"
        )

    meta_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not meta_exists:
        raise StoreFormatError(
            "file is a SQLite database but lacks a `meta` table; "
            "it does not appear to be a dataset-citation-finder store"
        )

    row = conn.execute(
        "SELECT schema_version FROM meta LIMIT 1"
    ).fetchone()
    if row is None:
        raise StoreFormatError("`meta` table exists but contains no row")

    file_version: int = row["schema_version"]
    if file_version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"store schema_version={file_version} is newer than this library "
            f"understands (max {CURRENT_SCHEMA_VERSION}); upgrade the library"
        )

    if file_version < CURRENT_SCHEMA_VERSION:
        if not migrate:
            raise MigrationRequired(
                f"store schema_version={file_version} is older than current "
                f"({CURRENT_SCHEMA_VERSION}); pass migrate=True to upgrade"
            )
        _run_migrations(conn, from_version=file_version)


def _run_migrations(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Apply migrations in order, each as its own transaction."""
    migrations = [m for m in discover_migrations() if m.from_version >= from_version]
    current = from_version
    for m in migrations:
        if m.from_version != current:
            # Gap in migration chain — refuse to proceed rather than skip.
            raise RuntimeError(
                f"migration gap: at version {current} but next migration "
                f"starts at {m.from_version}"
            )
        log.info("store: migrating v%d -> v%d (%s)", m.from_version, m.to_version, m.name)
        conn.execute("BEGIN")
        try:
            m.apply(conn)
            conn.execute("UPDATE meta SET schema_version = ?", (m.to_version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = m.to_version


# ---- describe_store -----------------------------------------------------

@dataclass
class StoreInfo:
    path: str
    schema_version: int
    library_version: str
    created_at: str
    created_by: str | None
    last_run_at: str | None
    run_count: int
    rows_per_source: dict[str, int]
    citations_count: int
    distinct_cited_dois: int


def describe_store(db_path: str | Path) -> StoreInfo:
    """Read-only summary of a store's contents. Does not modify the file."""
    conn = open_store(db_path, create=False, migrate=False, read_only=True)
    try:
        meta = conn.execute(
            "SELECT schema_version, library_version, created_at, created_by FROM meta LIMIT 1"
        ).fetchone()

        run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        last_run = conn.execute(
            "SELECT MAX(COALESCE(finished_at, started_at)) AS t FROM runs"
        ).fetchone()["t"]

        rows_per_source: dict[str, int] = {}
        for source_id, table in SOURCE_TABLES.items():
            try:
                rows_per_source[source_id] = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"
                ).fetchone()["c"]
            except sqlite3.OperationalError:
                rows_per_source[source_id] = 0

        citations_count = conn.execute(
            "SELECT COUNT(*) AS c FROM citations"
        ).fetchone()["c"]
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT cited_doi) AS c FROM citations"
        ).fetchone()["c"]

        return StoreInfo(
            path=str(db_path),
            schema_version=meta["schema_version"],
            library_version=meta["library_version"],
            created_at=meta["created_at"],
            created_by=meta["created_by"],
            last_run_at=last_run,
            run_count=run_count,
            rows_per_source=rows_per_source,
            citations_count=citations_count,
            distinct_cited_dois=distinct,
        )
    finally:
        conn.close()
