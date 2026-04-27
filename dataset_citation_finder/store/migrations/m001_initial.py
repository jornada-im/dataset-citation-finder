"""Initial schema (v0 -> v1).

Creates every table listed in `store.schema.INITIAL_DDL_STATEMENTS`. Used
by `open_store` when initializing a brand-new file. Re-running it on an
already-initialized file is a no-op (every statement uses IF NOT EXISTS).
"""

from __future__ import annotations

import sqlite3

from ..schema import INITIAL_DDL_STATEMENTS

from_version = 0
to_version = 1


def apply(conn: sqlite3.Connection) -> None:
    # NOTE: use execute(), not executescript() — the latter issues an
    # implicit COMMIT and would prematurely close the init transaction.
    for stmt in INITIAL_DDL_STATEMENTS:
        conn.execute(stmt)
