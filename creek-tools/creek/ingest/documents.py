"""Document file ingestor for the Creek ingest pipeline.

Ingests DOCX, PDF, HTML, TXT, and RTF files by converting them to
markdown fragments. Uses format-specific libraries for extraction:

- **DOCX**: ``python-docx`` for paragraphs, tables, and headings
- **PDF**: ``pdfminer.six`` for text extraction with scanned detection
- **HTML**: ``markdownify`` for direct HTML-to-markdown conversion
- **TXT**: Heuristic structure detection wrapping

Exports:
    DocumentIngestor: Concrete ``Ingestor`` subclass for document files.
    _parse_docx_to_markdown: Convert DOCX bytes to markdown string.
    _parse_pdf_to_text: Extract text from PDF bytes.
    _parse_html_to_markdown: Convert HTML string to markdown.
    _wrap_txt_with_structure: Wrap plain text with heuristic structure.
    _detect_scanned_pdf: Check if a PDF is likely scanned (image-only).
    _extract_docx_metadata: Extract metadata from DOCX bytes.
    _extract_pdf_metadata: Extract metadata from PDF bytes.
    _infer_document_platform: Map file extension to SourcePlatform.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_encoding,
    normalize_timestamp,
)
from creek.models import SourcePlatform

logger = logging.getLogger(__name__)

# ---- Constants ----

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".docx", ".pdf", ".html", ".htm", ".txt", ".rtf"}
)
"""File extensions supported by the DocumentIngestor."""

_SCANNED_TEXT_THRESHOLD = 100
"""Minimum character count to consider a multi-page PDF as text-based."""

_DOCUMENT_PLATFORM_EXTENSIONS: frozenset[str] = frozenset({".docx", ".pdf", ".rtf"})
"""Extensions that map to the DOCUMENT source platform."""


# ---- Helper Functions ----


def _parse_html_to_markdown(html: str) -> str:
    """Convert an HTML string to clean ATX-style markdown.

    Uses ``markdownify`` with ATX heading style for consistent output.

    Args:
        html: The HTML content to convert.

    Returns:
        A markdown-formatted string.

    Raises:
        ImportError: If ``markdownify`` is not installed. Install with
            ``pip install creek-tools[documents]``.
    """
    try:
        import markdownify
    except ImportError as exc:
        msg = (
            "markdownify is required for HTML ingestion. "
            "Install with: pip install creek-tools[documents]"
        )
        raise ImportError(msg) from exc
    result: str = markdownify.markdownify(html, heading_style="ATX")
    return result


def _wrap_txt_with_structure(text: str) -> str:
    """Wrap plain text with heuristic structure detection.

    Detects potential titles (first non-empty line) and preserves
    list items. Returns the text with minimal markdown formatting.

    Args:
        text: The plain text content.

    Returns:
        Text with basic markdown structure, or empty string for blank input.
    """
    if not text.strip():
        return ""

    lines = text.strip().split("\n")
    result_lines: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped and not stripped.startswith("-"):
            # Treat first non-empty line as a heading
            result_lines.append(f"# {stripped}")
        else:
            result_lines.append(line)

    return "\n".join(result_lines)


def _extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfminer.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        Extracted text content.
    """
    from pdfminer.high_level import (
        extract_text,
    )

    result: str = extract_text(io.BytesIO(pdf_bytes))
    return result


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Count the number of pages in a PDF.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        The number of pages in the PDF.
    """
    from pdfminer.pdfpage import (
        PDFPage,
    )

    return sum(1 for _ in PDFPage.get_pages(io.BytesIO(pdf_bytes)))


def _parse_pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes.

    Delegates to ``_extract_pdf_text_from_bytes`` for the actual extraction.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        Extracted text content.
    """
    return _extract_pdf_text_from_bytes(pdf_bytes)


def _detect_scanned_pdf(text: str, page_count: int) -> bool:
    """Detect if a PDF is likely scanned (image-only).

    A PDF is flagged as scanned if it has multiple pages but the
    extracted text is shorter than the threshold (100 characters).

    Args:
        text: The extracted text from the PDF.
        page_count: The number of pages in the PDF.

    Returns:
        True if the PDF appears to be scanned.
    """
    return page_count > 1 and len(text.strip()) < _SCANNED_TEXT_THRESHOLD


def _parse_docx_to_markdown(docx_bytes: bytes) -> str:
    """Convert DOCX bytes to a markdown string.

    Iterates through paragraphs and tables in the document, converting
    headings to ATX-style markdown and tables to pipe-delimited format.

    Args:
        docx_bytes: Raw DOCX file bytes.

    Returns:
        A markdown-formatted string.
    """
    from docx import (
        Document as DocxDocument,
    )

    doc = DocxDocument(io.BytesIO(docx_bytes))
    parts: list[str] = []

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            _convert_paragraph(element, doc, parts)
        elif tag == "tbl":
            _convert_table(element, doc, parts)

    return "\n\n".join(parts)


def _convert_paragraph(element: Any, doc: Any, parts: list[str]) -> None:
    """Convert a DOCX paragraph element to markdown and append to parts.

    Args:
        element: The paragraph XML element.
        doc: The python-docx Document object.
        parts: The list of markdown parts to append to.
    """
    from docx.text.paragraph import Paragraph

    para = Paragraph(element, doc)
    text = para.text.strip()
    if not text:
        return

    style_name = para.style.name if para.style else ""
    heading_level = _get_heading_level(style_name)

    if heading_level > 0:
        prefix = "#" * heading_level
        parts.append(f"{prefix} {text}")
    else:
        parts.append(text)


def _get_heading_level(style_name: str) -> int:
    """Extract heading level from a DOCX style name.

    Args:
        style_name: The paragraph style name (e.g., 'Heading 1').

    Returns:
        The heading level (1-6), or 0 if not a heading.
    """
    if style_name.startswith("Heading"):
        try:
            return int(style_name.split()[-1])
        except (ValueError, IndexError):
            return 0
    return 0


def _convert_table(element: Any, doc: Any, parts: list[str]) -> None:
    """Convert a DOCX table element to markdown and append to parts.

    Args:
        element: The table XML element.
        doc: The python-docx Document object.
        parts: The list of markdown parts to append to.
    """
    from docx.table import Table

    table = Table(element, doc)
    rows = table.rows
    if not rows:
        return

    table_lines: list[str] = []
    for i, row in enumerate(rows):
        cells = [cell.text.strip() for cell in row.cells]
        table_lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            table_lines.append("| " + " | ".join("---" for _ in cells) + " |")

    parts.append("\n".join(table_lines))


def _extract_docx_metadata(docx_bytes: bytes) -> dict[str, Any]:
    """Extract metadata from DOCX core properties.

    Extracts author, title, and creation date from the document's
    core properties.

    Args:
        docx_bytes: Raw DOCX file bytes.

    Returns:
        A dict with author, title, and created keys.
    """
    from docx import (
        Document as DocxDocument,
    )

    doc = DocxDocument(io.BytesIO(docx_bytes))
    props = doc.core_properties

    metadata: dict[str, Any] = {}
    if props.author:
        metadata["author"] = props.author
    if props.title:
        metadata["title"] = props.title
    if props.created:
        metadata["created_date"] = props.created.isoformat()

    return metadata


def _extract_pdf_metadata_from_bytes(pdf_bytes: bytes) -> dict[str, Any]:
    """Extract metadata from PDF document info dictionary.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        A dict with available metadata fields.
    """
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfparser import PDFParser

    metadata: dict[str, Any] = {}
    try:
        parser = PDFParser(io.BytesIO(pdf_bytes))
        pdf_doc = PDFDocument(parser)
        for info in pdf_doc.info:
            if isinstance(info, dict):
                for key in ("Author", "Title", "CreationDate"):
                    value = info.get(key)
                    if value is not None:
                        decoded = (
                            value.decode("utf-8", errors="replace")
                            if isinstance(value, bytes)
                            else str(value)
                        )
                        metadata[key.lower()] = decoded
    except Exception:
        logger.debug("Could not extract PDF metadata")

    return metadata


def _extract_pdf_metadata(pdf_bytes: bytes) -> dict[str, Any]:
    """Extract metadata from PDF bytes.

    Delegates to ``_extract_pdf_metadata_from_bytes``.

    Args:
        pdf_bytes: Raw PDF file bytes.

    Returns:
        A dict with available metadata fields.
    """
    return _extract_pdf_metadata_from_bytes(pdf_bytes)


def _infer_document_platform(extension: str) -> SourcePlatform:
    """Map a file extension to a SourcePlatform value.

    DOCX, PDF, and RTF map to ``DOCUMENT``. HTML and TXT map to ``OTHER``.

    Args:
        extension: The file extension (e.g., '.docx').

    Returns:
        The corresponding SourcePlatform enum value.
    """
    if extension in _DOCUMENT_PLATFORM_EXTENSIONS:
        return SourcePlatform.DOCUMENT
    return SourcePlatform.OTHER


# ---- DocumentIngestor ----


class DocumentIngestor(Ingestor):
    """Ingestor for common document formats (DOCX, PDF, HTML, TXT, RTF).

    Discovers files with supported extensions, parses them using
    format-specific libraries, converts to markdown, and generates
    Creek-compatible frontmatter with metadata extraction.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all document files at the given source path (recursively).

        If ``source_path`` is a file with a supported extension, returns
        a single-element list. If it is a directory, recursively searches
        for files with supported extensions. Returns empty list for
        nonexistent paths.

        Args:
            source_path: A file or directory path to search.

        Returns:
            A list of ``RawDocument`` objects for each discovered file.
        """
        if not source_path.exists():
            return []

        if source_path.is_file():
            return self._discover_single_file(source_path)

        return self._discover_directory(source_path)

    def _discover_single_file(self, file_path: Path) -> list[RawDocument]:
        """Discover a single document file.

        Args:
            file_path: Path to the file.

        Returns:
            A single-element list if the file has a supported extension.
        """
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return []

        return [self._read_file(file_path)]

    def _discover_directory(self, dir_path: Path) -> list[RawDocument]:
        """Recursively discover all document files in a directory.

        Args:
            dir_path: Directory path to search.

        Returns:
            A list of RawDocument objects for each document file found.
        """
        docs: list[RawDocument] = [
            self._read_file(file_path)
            for file_path in sorted(dir_path.rglob("*"))
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        return docs

    def _read_file(self, file_path: Path) -> RawDocument:
        """Read a file into a RawDocument.

        Args:
            file_path: Path to the file.

        Returns:
            A RawDocument instance.
        """
        raw_bytes = file_path.read_bytes()
        _text, encoding = normalize_encoding(raw_bytes)
        return RawDocument(
            path=file_path,
            content=raw_bytes,
            metadata={
                "source_type": "document",
                "file_type": file_path.suffix.lower(),
            },
            detected_encoding=encoding,
        )

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw document, routing to format-specific handlers.

        Dispatches to DOCX, PDF, HTML, or TXT parsers based on the
        file extension. Extracts metadata where available.

        Args:
            raw: The raw document to parse.

        Returns:
            A single-element list containing the parsed fragment.
        """
        file_type = raw.metadata.get("file_type", raw.path.suffix.lower())
        text, encoding = normalize_encoding(raw.content)

        content = self._extract_content(file_type, raw.content, text)
        metadata = self._extract_metadata(file_type, raw.content, encoding)
        timestamp = self._resolve_timestamp(metadata, raw.path)

        return [
            ParsedFragment(
                content=content,
                metadata=metadata,
                source_path=str(raw.path),
                timestamp=timestamp,
            )
        ]

    def _extract_content(self, file_type: str, raw_bytes: bytes, text: str) -> str:
        """Extract content based on file type.

        Args:
            file_type: The file extension.
            raw_bytes: Raw file bytes.
            text: Decoded text content.

        Returns:
            Extracted content string.
        """
        if file_type == ".docx":
            return _parse_docx_to_markdown(raw_bytes)
        if file_type == ".pdf":
            return self._extract_pdf_content(raw_bytes)
        if file_type in {".html", ".htm"}:
            return _parse_html_to_markdown(text)
        if file_type == ".txt":
            return _wrap_txt_with_structure(text)
        # RTF and other formats: return raw text
        return text

    def _extract_pdf_content(self, raw_bytes: bytes) -> str:
        """Extract text content from PDF bytes with scanned detection.

        Args:
            raw_bytes: Raw PDF file bytes.

        Returns:
            Extracted text content.
        """
        return _parse_pdf_to_text(raw_bytes)

    def _extract_metadata(
        self, file_type: str, raw_bytes: bytes, encoding: str
    ) -> dict[str, Any]:
        """Extract metadata based on file type.

        Args:
            file_type: The file extension.
            raw_bytes: Raw file bytes.
            encoding: Detected character encoding.

        Returns:
            Metadata dict with file_type, source_encoding, and format-specific fields.
        """
        metadata: dict[str, Any] = {
            "file_type": file_type,
            "source_encoding": encoding,
        }

        if file_type == ".docx":
            metadata.update(_extract_docx_metadata(raw_bytes))
        elif file_type == ".pdf":
            self._add_pdf_metadata(raw_bytes, metadata)

        return metadata

    def _add_pdf_metadata(self, raw_bytes: bytes, metadata: dict[str, Any]) -> None:
        """Add PDF-specific metadata including scanned detection.

        Args:
            raw_bytes: Raw PDF file bytes.
            metadata: Metadata dict to update.
        """
        pdf_meta = _extract_pdf_metadata(raw_bytes)
        metadata.update(pdf_meta)

        try:
            text = _parse_pdf_to_text(raw_bytes)
            page_count = _count_pdf_pages(raw_bytes)
            if _detect_scanned_pdf(text, page_count):
                metadata["scanned"] = True
        except Exception:
            logger.debug("Could not check for scanned PDF")

    def _resolve_timestamp(self, metadata: dict[str, Any], file_path: Path) -> datetime:
        """Resolve a timestamp from metadata or filesystem.

        Checks metadata for created_date first, then falls back to
        the file's modification time.

        Args:
            metadata: Fragment metadata dict.
            file_path: Path to the source file.

        Returns:
            A timezone-aware datetime.
        """
        created = metadata.get("created_date")
        if created is not None:
            try:
                return normalize_timestamp(str(created), None)
            except ValueError:
                logger.warning("Invalid metadata timestamp: %s", created)

        # Fall back to file modification time
        mtime = file_path.stat().st_mtime
        ts_string = datetime.fromtimestamp(mtime).isoformat()
        return normalize_timestamp(ts_string, None)

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Return the fragment content as markdown.

        Content is already converted to markdown during parsing, so
        this method returns it as-is.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            The markdown content string.
        """
        return fragment.content

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate Creek-compatible YAML frontmatter for a document fragment.

        Builds frontmatter with type, title, source (platform, original_file,
        original_encoding), created timestamp, and optional author/scanned
        fields from metadata.

        Args:
            fragment: The parsed fragment with metadata.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        file_type = fragment.metadata.get("file_type", "")
        platform = _infer_document_platform(file_type)
        title = fragment.metadata.get("title", Path(fragment.source_path).stem)

        source: dict[str, Any] = {
            "platform": platform,
            "original_file": fragment.source_path,
            "original_encoding": fragment.metadata.get("source_encoding", "utf-8"),
        }

        # Add optional metadata fields
        if "author" in fragment.metadata:
            source["author"] = fragment.metadata["author"]
        if fragment.metadata.get("scanned"):
            source["scanned"] = True

        return {
            "type": "fragment",
            "title": title,
            "source": source,
            "created": fragment.timestamp.isoformat(),
        }
