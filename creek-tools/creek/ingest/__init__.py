"""Creek ingest package --- registry of available ingestors and base classes.

This package provides the abstract ``Ingestor`` base class and shared
utilities for building source-specific ingestors.  Concrete ingestor
classes are registered declaratively in the ``INGESTOR_REGISTRY`` dict
literal below.

Exports:
    INGESTOR_REGISTRY: A dict mapping ingestor names to their classes.
    RawDocument: Pydantic model for raw document data.
    ParsedFragment: Pydantic model for parsed fragment data.
    IngestResult: Pydantic model for ingest pipeline results.
    Ingestor: Abstract base class for all ingestors.
    ChatGPTIngestor: Concrete ingestor for ChatGPT conversation exports.
    ClaudeIngestor: Concrete ingestor for Claude conversation exports.
    DiscordIngestor: Concrete ingestor for Discord message exports.
    GenericIngestor: Fallback ingestor for unrecognized file formats.
    MarkdownIngestor: Concrete ingestor for plain Markdown files.
"""

from creek.ingest.base import Ingestor, IngestResult, ParsedFragment, RawDocument
from creek.ingest.chatgpt import ChatGPTIngestor
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.discord import DiscordIngestor
from creek.ingest.generic import GenericIngestor
from creek.ingest.markdown import MarkdownIngestor

# Registry mapping ingestor names to their concrete classes.
# To add a new ingestor, import its class above and add an entry here.
INGESTOR_REGISTRY: dict[str, type[Ingestor]] = {
    "chatgpt": ChatGPTIngestor,
    "claude": ClaudeIngestor,
    "discord": DiscordIngestor,
    "generic": GenericIngestor,
    "markdown": MarkdownIngestor,
}

__all__ = [
    "INGESTOR_REGISTRY",
    "ChatGPTIngestor",
    "ClaudeIngestor",
    "DiscordIngestor",
    "GenericIngestor",
    "IngestResult",
    "Ingestor",
    "MarkdownIngestor",
    "ParsedFragment",
    "RawDocument",
]
