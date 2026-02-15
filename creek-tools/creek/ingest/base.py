"""Abstract Ingestor base class and shared utilities for the Creek ingest pipeline.

This module provides:

- **Pydantic models**: ``RawDocument``, ``ParsedFragment``, ``ProvenanceEntry``,
  and ``IngestResult`` for structured data flow through the ingest pipeline.
- **Utility functions**: ``normalize_encoding``, ``normalize_timestamp``,
  ``generate_fragment_id``, and ``create_provenance_entry`` for common
  ingest operations.
- **Abstract base class**: ``Ingestor`` defining the four-stage pipeline
  (discover, parse, convert, frontmatter) with a concrete ``ingest()``
  orchestrator.
"""

from __future__ import annotations

import abc
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — needed at runtime by Pydantic
from typing import Any
from zoneinfo import ZoneInfo

import chardet
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---- Constants ----

LA_TZ = ZoneInfo("America/Los_Angeles")
"""Target timezone for all normalized timestamps."""

# ---- Pydantic Models ----


class RawDocument(BaseModel):
    """A raw document discovered by an ingestor before parsing.

    Holds the file path, raw byte content, arbitrary metadata, and the
    detected character encoding.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    """Filesystem path to the source file."""

    content: bytes
    """Raw byte content of the file."""

    metadata: dict[str, Any]
    """Arbitrary metadata attached during discovery."""

    detected_encoding: str
    """Character encoding detected or declared for this document."""


class ParsedFragment(BaseModel):
    """A structured content fragment extracted from a raw document.

    Represents one logical unit of content after parsing, with its
    source provenance and timestamp.
    """

    content: str
    """The extracted text content."""

    metadata: dict[str, Any]
    """Arbitrary metadata from parsing (e.g., headers, tags)."""

    source_path: str
    """Path to the original source file."""

    timestamp: datetime
    """Timestamp associated with this fragment."""


class ProvenanceEntry(BaseModel):
    """A structured provenance record for auditing ingest operations.

    Tracks which ingestor processed which source file, when, and whether
    the operation succeeded.
    """

    source_path: str
    """Path to the source file that was ingested."""

    ingestor_name: str
    """Name of the ingestor class that processed this file."""

    timestamp: datetime
    """When the ingest operation occurred."""

    fragment_id: str
    """The generated fragment ID for this entry."""

    status: str
    """Status of the ingest operation (e.g., 'success', 'error', 'skipped')."""


class IngestResult(BaseModel):
    """Result of a complete ingest pipeline run.

    Collects all parsed fragments, provenance entries, and any error
    messages produced during the ingest process.
    """

    fragments: list[ParsedFragment] = Field(default_factory=list)
    """Parsed fragments produced by the ingest pipeline."""

    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    """Provenance records for auditing."""

    errors: list[str] = Field(default_factory=list)
    """Error messages collected during ingest."""


# ---- Shared Utility Functions ----


def normalize_encoding(raw_bytes: bytes) -> tuple[str, str]:
    """Detect the encoding of raw bytes and convert to UTF-8 text.

    Uses ``chardet`` for encoding detection. Empty input returns an
    empty string with ``"utf-8"`` as the detected encoding.

    Args:
        raw_bytes: The raw bytes to detect and decode.

    Returns:
        A tuple of ``(decoded_text, detected_encoding)``.
    """
    if not raw_bytes:
        return "", "utf-8"

    detection = chardet.detect(raw_bytes)
    encoding = detection.get("encoding") or "utf-8"

    text = raw_bytes.decode(encoding, errors="replace")
    return text, encoding


def normalize_timestamp(ts_string: str, source_tz: str | None) -> datetime:
    """Parse a timestamp string and normalize to America/Los_Angeles.

    Supports ISO 8601 formats, date-only strings, and common datetime
    formats. If the timestamp is naive (no timezone info), ``source_tz``
    is used to localize it; if ``source_tz`` is also ``None``, UTC is
    assumed.

    Args:
        ts_string: The timestamp string to parse.
        source_tz: Optional IANA timezone name for naive timestamps.

    Returns:
        A timezone-aware ``datetime`` in America/Los_Angeles.

    Raises:
        ValueError: If the timestamp string cannot be parsed.
    """
    parsed = _parse_timestamp_string(ts_string)
    localized = _localize_naive_timestamp(parsed, source_tz)
    return localized.astimezone(LA_TZ)


def _parse_timestamp_string(ts_string: str) -> datetime:
    """Parse a timestamp string into a datetime object.

    Tries ISO 8601 format first, then falls back to common formats.

    Args:
        ts_string: The timestamp string to parse.

    Returns:
        A datetime object (may be naive or aware).

    Raises:
        ValueError: If none of the known formats match.
    """
    # Try ISO 8601 first (handles timezone offsets)
    try:
        return datetime.fromisoformat(ts_string)
    except ValueError:
        pass

    # Try common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(ts_string, fmt)
        except ValueError:
            continue

    msg = f"Unable to parse timestamp: {ts_string}"
    raise ValueError(msg)


def _localize_naive_timestamp(dt: datetime, source_tz: str | None) -> datetime:
    """Attach timezone info to a naive datetime.

    If the datetime is already timezone-aware, returns it unchanged.
    If naive, uses ``source_tz`` or defaults to UTC.

    Args:
        dt: The datetime to localize.
        source_tz: Optional IANA timezone name.

    Returns:
        A timezone-aware datetime.
    """
    if dt.tzinfo is not None:
        return dt

    tz = ZoneInfo(source_tz) if source_tz is not None else UTC
    return dt.replace(tzinfo=tz)


def generate_fragment_id(source: str, timestamp: datetime, content: str) -> str:
    """Generate a deterministic fragment ID from source, timestamp, and content.

    Computes a SHA-256 hash of the concatenated inputs and returns the
    first 12 hex characters prefixed with ``frag-``.

    Args:
        source: The source identifier (e.g., file path).
        timestamp: The fragment timestamp.
        content: The fragment text content.

    Returns:
        A deterministic ID string in the format ``frag-XXXXXXXXXXXX``.
    """
    hash_input = f"{source}:{timestamp.isoformat()}:{content}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"frag-{digest}"


def create_provenance_entry(
    source_path: str,
    ingestor_name: str,
    timestamp: datetime,
    fragment_id: str,
    status: str,
) -> ProvenanceEntry:
    """Create a structured provenance record for an ingest operation.

    Args:
        source_path: Path to the source file.
        ingestor_name: Name of the ingestor class.
        timestamp: When the operation occurred.
        fragment_id: The generated fragment ID.
        status: Operation status (e.g., 'success', 'error', 'skipped').

    Returns:
        A ``ProvenanceEntry`` instance.
    """
    return ProvenanceEntry(
        source_path=source_path,
        ingestor_name=ingestor_name,
        timestamp=timestamp,
        fragment_id=fragment_id,
        status=status,
    )


# ---- Abstract Base Class ----


class Ingestor(abc.ABC):
    """Abstract base class for all Creek ingestors.

    Defines the four-stage ingest pipeline:

    1. **discover** — find all files/records at a source path
    2. **parse** — extract structured content from a raw document
    3. **convert_to_markdown** — convert a parsed fragment to clean Markdown
    4. **generate_frontmatter** — produce YAML frontmatter metadata

    The concrete ``ingest()`` method orchestrates these stages and collects
    results, provenance, and errors into an ``IngestResult``.

    Subclasses must implement all four abstract methods.
    """

    @abc.abstractmethod
    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all files or records at the given source path.

        Args:
            source_path: The directory or file path to search.

        Returns:
            A list of ``RawDocument`` objects found at the source.
        """

    @abc.abstractmethod
    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract structured content from a raw document.

        Args:
            raw: The raw document to parse.

        Returns:
            A list of ``ParsedFragment`` objects extracted from the document.
        """

    @abc.abstractmethod
    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to clean Markdown.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string.
        """

    @abc.abstractmethod
    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a parsed fragment.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """

    def ingest(self, source_path: Path) -> IngestResult:
        """Orchestrate the full ingest pipeline: discover, parse, convert, frontmatter.

        Calls ``discover()`` to find documents, then for each document calls
        ``parse()`` to extract fragments. For each fragment, calls
        ``convert_to_markdown()`` and ``generate_frontmatter()``. Collects
        all results into an ``IngestResult``, handling errors gracefully.

        Args:
            source_path: The directory or file path to ingest from.

        Returns:
            An ``IngestResult`` containing fragments, provenance, and errors.
        """
        result = IngestResult()
        ingestor_name = type(self).__name__
        now = datetime.now(tz=LA_TZ)

        # Stage 1: Discover
        raw_docs = self._discover_safe(source_path, result)

        # Stages 2-4: Parse, Convert, Frontmatter
        for raw_doc in raw_docs:
            self._process_document(raw_doc, result, ingestor_name, now)

        return result

    def _discover_safe(
        self, source_path: Path, result: IngestResult
    ) -> list[RawDocument]:
        """Safely call discover(), catching and logging errors.

        Args:
            source_path: The path to discover documents at.
            result: The IngestResult to append errors to.

        Returns:
            A list of discovered RawDocuments, or empty on error.
        """
        try:
            return self.discover(source_path)
        except Exception as exc:
            result.errors.append(f"discover error: {exc}")
            logger.exception("Error during discover for %s", source_path)
            return []

    def _process_document(
        self,
        raw_doc: RawDocument,
        result: IngestResult,
        ingestor_name: str,
        now: datetime,
    ) -> None:
        """Process a single raw document through parse, convert, and frontmatter.

        Args:
            raw_doc: The raw document to process.
            result: The IngestResult to collect into.
            ingestor_name: The class name of this ingestor.
            now: The current timestamp for provenance.
        """
        fragments = self._parse_safe(raw_doc, result)
        for fragment in fragments:
            self._process_fragment(fragment, result, ingestor_name, now)

    def _parse_safe(
        self, raw_doc: RawDocument, result: IngestResult
    ) -> list[ParsedFragment]:
        """Safely call parse(), catching and logging errors.

        Args:
            raw_doc: The raw document to parse.
            result: The IngestResult to append errors to.

        Returns:
            A list of parsed fragments, or empty on error.
        """
        try:
            return self.parse(raw_doc)
        except Exception as exc:
            result.errors.append(f"parse error for {raw_doc.path}: {exc}")
            logger.exception("Error parsing %s", raw_doc.path)
            return []

    def _process_fragment(
        self,
        fragment: ParsedFragment,
        result: IngestResult,
        ingestor_name: str,
        now: datetime,
    ) -> None:
        """Process a single fragment through convert and frontmatter stages.

        Args:
            fragment: The parsed fragment to process.
            result: The IngestResult to collect into.
            ingestor_name: The class name of this ingestor.
            now: The current timestamp for provenance.
        """
        frag_id = generate_fragment_id(
            fragment.source_path, fragment.timestamp, fragment.content
        )

        # Stage 3: Convert to markdown
        markdown = self._convert_safe(fragment, result)

        # Stage 4: Generate frontmatter
        frontmatter = self._frontmatter_safe(fragment, result)

        if markdown is not None and frontmatter is not None:
            fragment.metadata["markdown"] = markdown
            fragment.metadata["frontmatter"] = frontmatter
            result.fragments.append(fragment)
            result.provenance.append(
                create_provenance_entry(
                    source_path=fragment.source_path,
                    ingestor_name=ingestor_name,
                    timestamp=now,
                    fragment_id=frag_id,
                    status="success",
                )
            )
        else:
            result.provenance.append(
                create_provenance_entry(
                    source_path=fragment.source_path,
                    ingestor_name=ingestor_name,
                    timestamp=now,
                    fragment_id=frag_id,
                    status="error",
                )
            )

    def _convert_safe(
        self, fragment: ParsedFragment, result: IngestResult
    ) -> str | None:
        """Safely call convert_to_markdown(), catching errors.

        Args:
            fragment: The fragment to convert.
            result: The IngestResult to append errors to.

        Returns:
            The markdown string, or None on error.
        """
        try:
            return self.convert_to_markdown(fragment)
        except Exception as exc:
            result.errors.append(f"convert error for {fragment.source_path}: {exc}")
            logger.exception("Error converting %s to markdown", fragment.source_path)
            return None

    def _frontmatter_safe(
        self, fragment: ParsedFragment, result: IngestResult
    ) -> dict[str, Any] | None:
        """Safely call generate_frontmatter(), catching errors.

        Args:
            fragment: The fragment to generate frontmatter for.
            result: The IngestResult to append errors to.

        Returns:
            The frontmatter dict, or None on error.
        """
        try:
            return self.generate_frontmatter(fragment)
        except Exception as exc:
            result.errors.append(f"frontmatter error for {fragment.source_path}: {exc}")
            logger.exception(
                "Error generating frontmatter for %s", fragment.source_path
            )
            return None
