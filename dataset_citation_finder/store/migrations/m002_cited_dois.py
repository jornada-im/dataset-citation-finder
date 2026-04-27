"""Add `cited_dois` and `doi_validations` tables (v1 -> v2).

`cited_dois` is the persistent registry of every DOI ever submitted as a
query input or enumerated under a prefix — the "in-scope" DOI set. Many
rows will have no matching citation edges in `citations`; that distinguishes
"uncited dataset" from "DOI we have not queried".

`doi_validations` is an append-only audit log of registry-validation events.
The latest status for each DOI is also denormalized onto `cited_dois`
(`validation_status`, `validated_at`, `validated_registrar`) for fast reads.
"""

from __future__ import annotations

import sqlite3

from ..schema import CITED_DOIS_DDL, DOI_VALIDATIONS_DDL

from_version = 1
to_version = 2


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(CITED_DOIS_DDL)
    conn.execute(DOI_VALIDATIONS_DDL)
