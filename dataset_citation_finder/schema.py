"""Internal record schemas.

Two shapes:

1. `LongRow` — one row per (cited_doi, citing_doi, source) tuple. This is
   what adapters emit and what the long-format output uses. Intentionally
   undeduplicated so provenance is auditable.

2. `GroupedRecord` — one record per unique citing work, produced by the
   merge engine from a collection of LongRow's. Carries `sources` list
   and per-source IDs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .utils import normalize_doi, utcnow


class LongRow(BaseModel):
    """A single source's attribution of a single citation edge.

    See design doc §7 (Long-format row).
    """

    cited_doi: str
    citing_doi: str
    source: str
    relation_type: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    # Free-form adapter-specific metadata; kept out of the default table but
    # retained so formatters / downstream users can reach it if they want.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cited_doi", "citing_doi", mode="before")
    @classmethod
    def _normalize(cls, v: str) -> str:
        n = normalize_doi(v)
        if n is None:
            raise ValueError(f"invalid DOI: {v!r}")
        return n


class GroupedRecord(BaseModel):
    """A merged record representing one unique citing work.

    See design doc §7 (Grouped citation record).
    """

    # The citing work itself.
    doi: str | None = None  # None only when we have a pre-DOI fallback match
    title: str | None = None
    year: int | None = None
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    abstract: str | None = None
    url: str | None = None

    # Provenance.
    sources: list[str] = Field(default_factory=list)
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    datacite_event_ids: list[str] = Field(default_factory=list)
    datacite_relation_types: list[str] = Field(default_factory=list)
    crossref_score: float | None = None

    # What was cited.
    cited_dois: list[str] = Field(default_factory=list)

    @field_validator("doi", mode="before")
    @classmethod
    def _normalize(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_doi(v)
