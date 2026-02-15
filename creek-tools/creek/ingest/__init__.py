"""Creek ingest package — registry of available ingestors and base classes.

This package provides the abstract ``Ingestor`` base class and shared
utilities for building source-specific ingestors. Concrete ingestor
implementations register themselves in the ``INGESTOR_REGISTRY`` dict.

Exports:
    INGESTOR_REGISTRY: A dict mapping ingestor names to their classes.
    RawDocument: Pydantic model for raw document data.
    ParsedFragment: Pydantic model for parsed fragment data.
    IngestResult: Pydantic model for ingest pipeline results.
    Ingestor: Abstract base class for all ingestors.
"""

from creek.ingest.base import Ingestor, IngestResult, ParsedFragment, RawDocument
from creek.ingest.discord import DiscordIngestor

INGESTOR_REGISTRY: dict[str, type[Ingestor]] = {
    "discord": DiscordIngestor,
}
"""Registry mapping ingestor names to their concrete classes.

Concrete ingestors should register themselves here upon import, e.g.::

    from creek.ingest import INGESTOR_REGISTRY
    INGESTOR_REGISTRY["claude"] = ClaudeIngestor
"""

__all__ = [
    "INGESTOR_REGISTRY",
    "DiscordIngestor",
    "IngestResult",
    "Ingestor",
    "ParsedFragment",
    "RawDocument",
]
