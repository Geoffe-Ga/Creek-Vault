"""Generic/fallback ingestor for unrecognized file formats.

Handles files that are not claimed by any specialized ingestor (Markdown,
ChatGPT, Claude, Discord).  Attempts text reading with multiple encoding
strategies (UTF-8, UTF-16, Latin-1, chardet fallback), skips binary files,
and routes unclassified content to ``01-Fragments/Unsorted/`` with
``source.platform: "unknown"`` in frontmatter.

Exports:
    GenericIngestor: Concrete ``Ingestor`` subclass for unclaimed files.
    _is_binary_content: Heuristic check for binary file content.
    _try_decode: Multi-strategy text decoding with fallback chain.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import chardet

from creek.ingest._authored_at import safe_file_modified_time
from creek.ingest.base import (
    LA_TZ,
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_encoding,
)

logger = logging.getLogger(__name__)

# File extensions claimed by specialized ingestors
# (.md for MarkdownIngestor, .json for ChatGPT/Claude/Discord ingestors)
_CLAIMED_EXTENSIONS: frozenset[str] = frozenset({".md", ".json"})

# Extensions rendered as plain text (no code block wrapping)
_PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".text", ".log"})

# Number of bytes to sample for binary content detection
_BINARY_CHECK_SIZE = 8192

# If more than 10% of bytes are non-text control chars, treat as binary
_BINARY_CONTROL_THRESHOLD = 0.10


def _is_binary_content(raw_bytes: bytes) -> bool:
    """Determine whether raw bytes represent binary (non-text) content.

    Uses two heuristics:
    1. Presence of null bytes (``\\x00``) — almost always binary.
    2. High ratio of non-text control characters (codes 0-8, 14-31).

    Args:
        raw_bytes: The raw bytes to check.

    Returns:
        ``True`` if the content appears to be binary, ``False`` otherwise.
    """
    if not raw_bytes:
        return False

    if b"\x00" in raw_bytes:
        return True

    sample = raw_bytes[:_BINARY_CHECK_SIZE]
    control_count = sum(1 for byte in sample if byte < 9 or (14 <= byte <= 31))
    return control_count / len(sample) > _BINARY_CONTROL_THRESHOLD


def _try_decode(raw_bytes: bytes) -> str | None:
    """Attempt to decode raw bytes using multiple encoding strategies.

    Tries encodings in order: UTF-8, UTF-16, Latin-1, then chardet
    detection as a final fallback. Returns ``None`` only if all
    strategies fail or the content is detected as binary.

    Args:
        raw_bytes: The raw bytes to decode.

    Returns:
        The decoded text string, or ``None`` if decoding fails.
    """
    if not raw_bytes:
        return ""

    # Try UTF-16 first if BOM is present (contains null bytes so check before binary)
    if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        with suppress(UnicodeDecodeError, ValueError):
            return raw_bytes.decode("utf-16")

    if _is_binary_content(raw_bytes):
        return None

    # Try UTF-8 first (most common encoding)
    with suppress(UnicodeDecodeError, ValueError):
        return raw_bytes.decode("utf-8")

    # Try chardet detection before falling back to Latin-1
    detected_encoding = chardet.detect(raw_bytes).get("encoding")
    if detected_encoding:
        with suppress(UnicodeDecodeError, ValueError, LookupError):
            return raw_bytes.decode(detected_encoding)

    # Latin-1 as final fallback (can decode any byte sequence)
    return raw_bytes.decode("latin-1")


class GenericIngestor(Ingestor):
    """Fallback ingestor for files not claimed by specialized ingestors.

    Discovers all files whose extensions are not in ``_CLAIMED_EXTENSIONS``,
    attempts multi-encoding text decoding, skips binary files, and routes
    successfully parsed content to ``01-Fragments/Unsorted/`` with
    ``source.platform: "unknown"``.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find files not claimed by any specialized ingestor.

        Recursively walks ``source_path``, skipping files with extensions
        in ``_CLAIMED_EXTENSIONS`` (e.g. ``.md``, ``.json``).

        Args:
            source_path: The directory to search.

        Returns:
            A list of ``RawDocument`` objects for unclaimed files.
        """
        if not source_path.exists():
            return []

        if source_path.is_file():
            return self._discover_single_file(source_path)

        return self._discover_directory(source_path)

    def _discover_single_file(self, file_path: Path) -> list[RawDocument]:
        """Check and read a single file if not claimed by a specialized ingestor.

        Args:
            file_path: Path to the file.

        Returns:
            A single-element list if the file is unclaimed, else empty.
        """
        if file_path.suffix.lower() in _CLAIMED_EXTENSIONS:
            return []

        raw_bytes = file_path.read_bytes()
        _text, encoding = normalize_encoding(raw_bytes)
        return [
            RawDocument(
                path=file_path,
                content=raw_bytes,
                metadata={"source_type": "generic"},
                detected_encoding=encoding,
            )
        ]

    def _discover_directory(self, dir_path: Path) -> list[RawDocument]:
        """Recursively discover unclaimed files in a directory.

        Args:
            dir_path: Directory path to search.

        Returns:
            A list of ``RawDocument`` objects for unclaimed files.
        """
        docs: list[RawDocument] = []
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in _CLAIMED_EXTENSIONS:
                continue

            raw_bytes = file_path.read_bytes()
            _text, encoding = normalize_encoding(raw_bytes)
            docs.append(
                RawDocument(
                    path=file_path,
                    content=raw_bytes,
                    metadata={"source_type": "generic"},
                    detected_encoding=encoding,
                )
            )
        return docs

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw document, skipping binary files.

        Attempts multi-encoding text decoding via ``_try_decode``.
        Binary files and empty files are skipped (return empty list).

        Args:
            raw: The raw document to parse.

        Returns:
            A single-element list with the parsed fragment, or empty
            if the file is binary or empty.
        """
        if not raw.content:
            return []

        text = _try_decode(raw.content)
        if text is None:
            logger.info("Skipping binary file: %s", raw.path)
            return []

        if not text.strip():
            return []

        now = datetime.now(tz=LA_TZ)
        # FEAT-031 (#263): GenericIngestor is the lowest-fidelity tier —
        # no in-band source date is available, so the file's mtime is
        # the only honest answer. ``None`` when the path is unreadable
        # (a synthetic ``RawDocument`` in tests, a vanished file mid-run).
        authored_at = safe_file_modified_time(raw.path)
        return [
            ParsedFragment(
                content=text,
                metadata={
                    "file_extension": raw.path.suffix.lower(),
                    "source_type": "generic",
                    "authored_at": authored_at,
                },
                source_path=str(raw.path),
                timestamp=now,
            )
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to Markdown.

        Plain text files (``.txt``, ``.text``, ``.log``) are rendered
        as-is. Other file types are wrapped in a fenced code block
        with the file extension as the language hint.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string.
        """
        ext = fragment.metadata.get("file_extension", "")
        if ext in _PLAIN_TEXT_EXTENSIONS:
            return fragment.content

        lang = ext.lstrip(".") if ext else ""
        return f"```{lang}\n{fragment.content}\n```"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter for an unclaimed file.

        Sets ``source.platform`` to ``"unknown"`` and routes to
        ``01-Fragments/Unsorted/``.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        title = Path(fragment.source_path).stem
        fm: dict[str, Any] = {
            "type": "fragment",
            "title": title,
            "source": {
                "platform": "unknown",
                "original_file": fragment.source_path,
            },
            "created": fragment.timestamp.isoformat(),
            "routing": "01-Fragments/Unsorted/",
        }
        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            fm["authored_at"] = authored_at.isoformat()
        return fm
