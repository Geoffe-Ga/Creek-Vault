"""Filter result model shared across all pre-ingestion filters.

Defines :class:`FilterResult`, the common return type for filter
operations.  Extracted into a private module to avoid circular imports
between ``__init__.py`` and concrete filter implementations.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FilterResult(BaseModel):
    """Result of applying a pre-ingestion filter to file content.

    Attributes:
        keep: Whether the file should be kept for ingestion.
        reason: Human-readable explanation when ``keep`` is ``False``.
        warnings: Non-blocking issues detected in the content.
    """

    keep: bool
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
