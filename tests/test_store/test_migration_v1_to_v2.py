"""Migration test: a store written by v0.2.0 (schema v1) opens cleanly under
the current library and grows the v2 tables on first open.

We simulate a v1 store by creating one normally and then forcing
schema_version back to 1 plus dropping the v2 tables.
"""

from __future__ import annotations

from dataset_citation_finder import (
    CURRENT_SCHEMA_VERSION,
    open_store,
)


def _make_v1_fixture(db_path):
    """Create a current store and downgrade it to v1 in place."""
    conn = open_store(db_path)
    conn.execute("DROP TABLE IF EXISTS doi_validations")
    conn.execute("DROP TABLE IF EXISTS cited_dois")
    conn.execute("UPDATE meta SET schema_version = 1")
    conn.close()


class TestV1ToV2Migration:

    def test_open_v1_store_runs_migration(self, tmp_path):
        db = tmp_path / "v1.sqlite"
        _make_v1_fixture(db)

        conn = open_store(db)
        try:
            version = conn.execute(
                "SELECT schema_version FROM meta"
            ).fetchone()["schema_version"]
            assert version == CURRENT_SCHEMA_VERSION
            assert version >= 2
            # New tables exist.
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "cited_dois" in tables
            assert "doi_validations" in tables
        finally:
            conn.close()

    def test_migration_idempotent(self, tmp_path):
        """Re-opening an already-migrated store is a no-op."""
        db = tmp_path / "v1.sqlite"
        _make_v1_fixture(db)
        open_store(db).close()
        # Open again; should not error.
        conn = open_store(db)
        try:
            assert conn.execute(
                "SELECT schema_version FROM meta"
            ).fetchone()["schema_version"] == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()
