"""Pre-ingestion content filters for the Creek clean pipeline.

Provides :class:`FilterResult`, the common return type for all filters,
and re-exports concrete filter implementations.

Exports:
    FilterResult: Pydantic model describing a filter decision.
    MarkdownFilter: Filter for markdown files before ingestion.
"""

from creek.clean.filters._result import FilterResult
from creek.clean.filters.markdown import MarkdownFilter

__all__ = [
    "FilterResult",
    "MarkdownFilter",
]
