"""Creek ingest package --- registry of available ingestors and base classes.

This package provides the abstract ``Ingestor`` base class and shared
utilities for building source-specific ingestors.  Concrete ingestor
classes are registered declaratively in the ``INGESTOR_REGISTRY`` dict
literal below.

Exports:
    INGESTOR_REGISTRY: A dict mapping ingestor names to their classes.
    INGESTOR_INPUT_EXPECTATIONS: What each ingestor's discover() reads.
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
    ARCHIVE_GUIDANCE,
    LEGACY_OFFICE_GUIDANCE,
    STRUCTURED_EXPORT_GUIDANCE,
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

INGESTOR_INPUT_EXPECTATIONS: dict[str, str] = {
    "chatgpt": (
        "a ChatGPT export JSON (conversations.json) sitting directly in the "
        "source directory; nested copies are not searched"
    ),
    "claude": (
        "a Claude export JSON (conversations.json) sitting directly in the "
        "source directory; nested copies are not searched"
    ),
    "code": (
        "README / CLAUDE.md / ADR notes and .py files anywhere under the "
        "source directory"
    ),
    "discord": "a Discord export laid out as messages/<channel_id>/messages.json",
    "document": (
        "files ending .docx, .pdf, .html, .htm, .txt or .rtf; a directory that "
        "looks like a Substack export is left for --type substack instead"
    ),
    "generic": (
        "any readable text file whose extension no specialised ingestor claims "
        "(.md and .json are claimed)"
    ),
    "image": "images ending .png, .jpg, .jpeg, .gif, .bmp, .tiff or .webp",
    "markdown": "files ending .md",
    "presentation": "presentations ending .pptx",
    "spreadsheet": "spreadsheets ending .xlsx or .csv",
    "substack": (
        "post HTML named <post_id>.<slug>.html (e.g. 164523.on-silt.html), "
        "optionally alongside posts.csv"
    ),
}
"""What each registered ingestor's ``discover()`` will actually read (#1574).

Read by ``creek.cli._warn_if_discovered_but_empty`` when the consent
preflight counted files an ingestor discovered none of. Without it the
operator is told only that zero fragments appeared, and the reason -- a
Substack post filename missing its leading post id, a ChatGPT export one
directory deeper than the walk looks -- stays in a ``logger.debug`` line no
CLI run prints.

Kept beside :data:`INGESTOR_REGISTRY` rather than on each ingestor class so
the two are read together: a new ingestor whose entry is missing here is
caught by the test asserting the two keysets are equal, not discovered later
by an operator holding an advisory with nothing actionable in it.
"""

__all__ = [
    "ARCHIVE_GUIDANCE",
    "INGESTOR_INPUT_EXPECTATIONS",
    "INGESTOR_REGISTRY",
    "LEGACY_OFFICE_GUIDANCE",
    "STRUCTURED_EXPORT_GUIDANCE",
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
