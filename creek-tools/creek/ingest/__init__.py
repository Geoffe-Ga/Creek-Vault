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
    CodeIngestor: Concrete ingestor for code repositories.
    DiscordIngestor: Concrete ingestor for Discord message exports.
    DocumentIngestor: Concrete ingestor for document files (DOCX, PDF, HTML, TXT).
    GenericIngestor: Fallback ingestor for unrecognized file formats.
    GoogleDriveDownloader: Read-only Google Drive download orchestrator.
    ImageIngestor: Concrete ingestor for image files via OCR.
    MarkdownIngestor: Concrete ingestor for plain Markdown files.
"""

from creek.ingest.base import (
    IngestedFragment,
    Ingestor,
    IngestResult,
    ParsedFragment,
    RawDocument,
    assemble_ingested_fragment,
)
from creek.ingest.chatgpt import ChatGPTIngestor
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.code import CodeIngestor
from creek.ingest.discord import DiscordIngestor
from creek.ingest.documents import DocumentIngestor
from creek.ingest.gdrive import (
    DownloadResult,
    DriveClient,
    DriveFile,
    GoogleApiUnavailableError,
    GoogleDriveDownloader,
    UnsupportedSourceError,
    route_to_ingestor,
)
from creek.ingest.generic import GenericIngestor
from creek.ingest.images import ImageIngestor
from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.presentations import PresentationIngestor
from creek.ingest.spreadsheets import SpreadsheetIngestor
from creek.ingest.substack import SubstackIngestor

# Registry mapping ingestor names to their concrete classes.
# To add a new ingestor, import its class above and add an entry here.
INGESTOR_REGISTRY: dict[str, type[Ingestor]] = {
    "chatgpt": ChatGPTIngestor,
    "claude": ClaudeIngestor,
    "code": CodeIngestor,
    "discord": DiscordIngestor,
    "document": DocumentIngestor,
    "generic": GenericIngestor,
    "image": ImageIngestor,
    "markdown": MarkdownIngestor,
    "presentation": PresentationIngestor,
    "spreadsheet": SpreadsheetIngestor,
    "substack": SubstackIngestor,
}

__all__ = [
    "INGESTOR_REGISTRY",
    "ChatGPTIngestor",
    "ClaudeIngestor",
    "CodeIngestor",
    "DiscordIngestor",
    "DocumentIngestor",
    "DownloadResult",
    "DriveClient",
    "DriveFile",
    "GenericIngestor",
    "GoogleApiUnavailableError",
    "GoogleDriveDownloader",
    "ImageIngestor",
    "IngestResult",
    "IngestedFragment",
    "Ingestor",
    "MarkdownIngestor",
    "ParsedFragment",
    "PresentationIngestor",
    "RawDocument",
    "SpreadsheetIngestor",
    "SubstackIngestor",
    "UnsupportedSourceError",
    "assemble_ingested_fragment",
    "route_to_ingestor",
]
