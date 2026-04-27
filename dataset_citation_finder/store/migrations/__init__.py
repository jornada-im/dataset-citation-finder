"""Schema migrations for the SQLite store.

Each migration is a numbered module (`001_initial.py`, `002_*.py`, …)
exposing:

    from_version: int
    to_version: int
    def apply(conn: sqlite3.Connection) -> None: ...

`apply` runs inside an outer transaction managed by the caller and must be
idempotent — re-running it after a partial failure must not corrupt state.
"""

from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Migration:
    name: str
    from_version: int
    to_version: int
    apply: Callable[[sqlite3.Connection], None]


def discover_migrations() -> list[Migration]:
    """Return all migrations in this package, ordered by `from_version`."""
    migrations: list[Migration] = []
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        migrations.append(
            Migration(
                name=mod_info.name,
                from_version=getattr(mod, "from_version"),
                to_version=getattr(mod, "to_version"),
                apply=getattr(mod, "apply"),
            )
        )
    migrations.sort(key=lambda m: m.from_version)
    return migrations
