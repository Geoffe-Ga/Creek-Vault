"""Creek ingest package — registry of available ingestors and base classes.

This package provides the abstract ``Ingestor`` base class and shared
utilities for building source-specific ingestors. All concrete ingestor
classes are registered in ``INGESTOR_REGISTRY`` via a single declarative
dict literal — no imperative ``INGESTOR_REGISTRY["key"] = Value`` lines.

Exports:
    INGESTOR_REGISTRY: A dict mapping ingestor names to their classes.
    RawDocument: Pydantic model for raw document data.
    ParsedFragment: Pydantic model for parsed fragment data.
    IngestResult: Pydantic model for ingest pipeline results.
    Ingestor: Abstract base class for all ingestors.
    ClaudeIngestor: Concrete ingestor for Claude conversation exports.
    DiscordIngestor: Concrete ingestor for Discord message exports.
    MarkdownIngestor: Concrete ingestor for plain Markdown files.
"""

from creek.ingest.base import Ingestor, IngestResult, ParsedFragment, RawDocument
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.discord import DiscordIngestor
from creek.ingest.markdown import MarkdownIngestor

INGESTOR_REGISTRY: dict[str, type[Ingestor]] = {
    "claude": ClaudeIngestor,
    "discord": DiscordIngestor,
    "markdown": MarkdownIngestor,
}
"""Registry mapping ingestor names to their concrete classes.

Built-in ingestors are registered automatically on import. To look up
a registered ingestor by name::

    from creek.ingest import INGESTOR_REGISTRY
    ingestor_cls = INGESTOR_REGISTRY["claude"]
"""

__all__ = [
    "INGESTOR_REGISTRY",
    "ClaudeIngestor",
    "DiscordIngestor",
    "IngestResult",
    "Ingestor",
    "MarkdownIngestor",
    "ParsedFragment",
    "RawDocument",
]
