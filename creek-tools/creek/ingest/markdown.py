"""Markdown file ingestor for the Creek ingest pipeline.

Ingests existing ``.md`` files by preserving their content, detecting or
merging YAML frontmatter, and normalizing formatting. Files that already
have frontmatter get Creek fields merged in as defaults (existing fields
take priority). Files without frontmatter receive fresh Creek frontmatter.

Exports:
    MarkdownIngestor: Concrete ``Ingestor`` subclass for markdown files.
    _detect_document_type: Classify content as journal, essay, technical, or notes.
    _infer_platform: Map document type and path to a ``SourcePlatform``.
    _merge_frontmatter: Merge Creek defaults with existing frontmatter.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_encoding,
    normalize_timestamp,
)
from creek.models import SourcePlatform

logger = logging.getLogger(__name__)

# ---- Pattern Constants ----

_JOURNAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\d{4}-\d{2}-\d{2}", re.MULTILINE),
    re.compile(r"(?i)\bdear diary\b"),
    re.compile(r"(?i)\btoday i\b"),
    re.compile(r"(?i)\breflect(?:ed|ing)?\b"),
]
"""Regex patterns that indicate journal-style content."""

_ESSAY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)^#{1,2}\s+introduction", re.MULTILINE),
    re.compile(r"(?i)^#{1,2}\s+conclusion", re.MULTILINE),
    re.compile(r"(?i)\bthesis\b"),
    re.compile(r"(?i)\bin this essay\b"),
]
"""Regex patterns that indicate essay-style content."""

_TECHNICAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"```\w+", re.MULTILINE),
    re.compile(r"(?i)\bapi\b"),
    re.compile(r"(?i)\bconfiguration\b"),
    re.compile(r"(?i)\bfunction\b"),
]
"""Regex patterns that indicate technical content."""

_JOURNAL_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)/daily/"),
    re.compile(r"(?i)/journal/"),
    re.compile(r"(?i)/diary/"),
]
"""Path patterns that indicate journal content."""

_ESSAY_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)/essay"),
    re.compile(r"(?i)/writing/"),
]
"""Path patterns that indicate essay content."""

# ---- Score Thresholds ----

_TYPE_SCORE_THRESHOLD = 2
"""Minimum pattern match count to classify as a specific document type."""


# ---- Helper Functions ----


def _detect_document_type(content: str) -> str:
    """Classify markdown content as journal, essay, technical, or notes.

    Uses regex pattern matching to score content against known patterns
    for each document type. Returns the type with the highest score
    above the threshold, or ``'notes'`` as the default.

    Args:
        content: The markdown content body to classify.

    Returns:
        One of ``'journal'``, ``'essay'``, ``'technical'``, or ``'notes'``.
    """
    if not content.strip():
        return "notes"

    scores: dict[str, int] = {
        "journal": _count_pattern_matches(content, _JOURNAL_PATTERNS),
        "essay": _count_pattern_matches(content, _ESSAY_PATTERNS),
        "technical": _count_pattern_matches(content, _TECHNICAL_PATTERNS),
    }

    best_type = max(scores, key=lambda k: scores[k])
    if scores[best_type] >= _TYPE_SCORE_THRESHOLD:
        return best_type

    return "notes"


def _count_pattern_matches(content: str, patterns: list[re.Pattern[str]]) -> int:
    """Count how many patterns match within the content.

    Args:
        content: The text to search.
        patterns: A list of compiled regex patterns.

    Returns:
        The number of patterns that matched at least once.
    """
    return sum(1 for p in patterns if p.search(content))


def _infer_platform(document_type: str, file_path: Path) -> SourcePlatform:
    """Infer the source platform from document type and file path.

    First checks the document type for a direct mapping. If the type
    is ``'notes'``, falls back to path-based heuristics (e.g., files
    in a ``daily/`` or ``journal/`` directory are classified as journal).

    Args:
        document_type: The detected document type (journal, essay, etc.).
        file_path: The path to the source markdown file.

    Returns:
        The inferred ``SourcePlatform`` enum value.
    """
    type_map: dict[str, SourcePlatform] = {
        "journal": SourcePlatform.JOURNAL,
        "essay": SourcePlatform.ESSAY,
        "technical": SourcePlatform.CODE,
    }

    if document_type in type_map:
        return type_map[document_type]

    return _infer_platform_from_path(file_path)


def _infer_platform_from_path(file_path: Path) -> SourcePlatform:
    """Infer platform from directory path patterns.

    Args:
        file_path: The path to check for known directory patterns.

    Returns:
        The inferred ``SourcePlatform`` enum value.
    """
    path_str = str(file_path)

    if any(p.search(path_str) for p in _JOURNAL_PATH_PATTERNS):
        return SourcePlatform.JOURNAL

    if any(p.search(path_str) for p in _ESSAY_PATH_PATTERNS):
        return SourcePlatform.ESSAY

    return SourcePlatform.OTHER


def _merge_frontmatter(
    creek_defaults: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Merge Creek-generated frontmatter with existing frontmatter.

    Creek fields act as defaults: they are only included if the
    corresponding key does not already exist in the existing
    frontmatter. Existing fields always take priority.

    Args:
        creek_defaults: Creek-generated frontmatter fields.
        existing: Existing frontmatter from the markdown file.

    Returns:
        A merged dict where existing fields override Creek defaults.
    """
    merged = dict(creek_defaults)
    merged.update(existing)
    return merged


def _extract_timestamp_from_frontmatter(fm_data: dict[str, Any]) -> str | None:
    """Extract a timestamp string from frontmatter date fields.

    Checks for ``date``, ``created``, and ``created_at`` fields in order.

    Args:
        fm_data: The parsed frontmatter dictionary.

    Returns:
        A timestamp string if found, or ``None``.
    """
    for key in ("date", "created", "created_at"):
        value = fm_data.get(key)
        if value is not None:
            return str(value)
    return None


def _get_file_creation_timestamp(path: Path) -> datetime:
    """Get the file creation timestamp from filesystem metadata.

    Falls back to modification time if creation time is not available.
    Returns a timezone-aware datetime in America/Los_Angeles.

    Args:
        path: The file path to inspect.

    Returns:
        A timezone-aware datetime from the file's metadata.
    """
    stat = path.stat()
    # Use birth time on macOS, fall back to mtime
    ctime = getattr(stat, "st_birthtime", stat.st_mtime)
    ts_string = datetime.fromtimestamp(ctime).isoformat()
    return normalize_timestamp(ts_string, None)


# ---- MarkdownIngestor ----


class MarkdownIngestor(Ingestor):
    """Ingestor for existing Markdown (``.md``) files.

    Discovers ``.md`` files recursively, parses their content and
    frontmatter using ``python-frontmatter``, preserves existing
    formatting, and generates Creek-compatible frontmatter with a
    merge strategy that respects existing fields.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all ``.md`` files at the given source path (recursively).

        If ``source_path`` is a file, returns a single-element list for
        that file. If it is a directory, recursively globs for ``*.md``.
        If the path does not exist, returns an empty list.

        Args:
            source_path: A file or directory path to search.

        Returns:
            A list of ``RawDocument`` objects for each discovered file.
        """
        if not source_path.exists():
            return []

        if source_path.is_file():
            return self._read_single_file(source_path)

        return self._read_directory(source_path)

    def _read_single_file(self, file_path: Path) -> list[RawDocument]:
        """Read a single markdown file into a RawDocument.

        Args:
            file_path: Path to the markdown file.

        Returns:
            A single-element list containing the RawDocument.
        """
        raw_bytes = file_path.read_bytes()
        _text, encoding = normalize_encoding(raw_bytes)
        return [
            RawDocument(
                path=file_path,
                content=raw_bytes,
                metadata={"source_type": "markdown"},
                detected_encoding=encoding,
            )
        ]

    def _read_directory(self, dir_path: Path) -> list[RawDocument]:
        """Recursively discover all .md files in a directory.

        Args:
            dir_path: Directory path to search.

        Returns:
            A list of RawDocument objects for each .md file found.
        """
        docs: list[RawDocument] = []
        for md_file in sorted(dir_path.rglob("*.md")):
            raw_bytes = md_file.read_bytes()
            _text, encoding = normalize_encoding(raw_bytes)
            docs.append(
                RawDocument(
                    path=md_file,
                    content=raw_bytes,
                    metadata={"source_type": "markdown"},
                    detected_encoding=encoding,
                )
            )
        return docs

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw markdown document, extracting frontmatter and content.

        Uses ``python-frontmatter`` to separate YAML frontmatter from
        the markdown body. Detects document type from content patterns
        and extracts a timestamp from frontmatter or filesystem metadata.

        Args:
            raw: The raw document to parse.

        Returns:
            A single-element list containing the parsed fragment.
        """
        text, _encoding = normalize_encoding(raw.content)
        fm_data, content = self._parse_frontmatter(text)
        document_type = _detect_document_type(content)
        timestamp = self._resolve_timestamp(fm_data, raw.path)

        return [
            ParsedFragment(
                content=content,
                metadata={
                    "existing_frontmatter": fm_data,
                    "document_type": document_type,
                },
                source_path=str(raw.path),
                timestamp=timestamp,
            )
        ]

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        """Parse YAML frontmatter from markdown text.

        Handles malformed frontmatter gracefully by treating the entire
        text as content if parsing fails.

        Args:
            text: The full markdown text (possibly with frontmatter).

        Returns:
            A tuple of (frontmatter_dict, content_body).
        """
        try:
            post = frontmatter.loads(text)
            return dict(post.metadata), post.content
        except Exception:
            logger.warning("Failed to parse frontmatter, treating as plain content")
            return {}, text

    def _resolve_timestamp(self, fm_data: dict[str, Any], file_path: Path) -> datetime:
        """Resolve a timestamp from frontmatter or filesystem metadata.

        Checks frontmatter fields first (date, created, created_at),
        then falls back to the file's creation/modification time.

        Args:
            fm_data: Parsed frontmatter dictionary.
            file_path: Path to the source file.

        Returns:
            A timezone-aware datetime.
        """
        fm_ts = _extract_timestamp_from_frontmatter(fm_data)
        if fm_ts is not None:
            try:
                return normalize_timestamp(fm_ts, None)
            except ValueError:
                logger.warning("Invalid frontmatter timestamp: %s", fm_ts)

        return _get_file_creation_timestamp(file_path)

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Return the fragment content as-is (already markdown).

        Since the source files are already markdown, this method simply
        returns the content without modification, preserving all
        existing formatting.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            The markdown content string.
        """
        return fragment.content

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate Creek-compatible YAML frontmatter for a parsed fragment.

        Builds Creek default fields (type, title, source, created) and
        merges them with any existing frontmatter from the original file.
        Existing fields take priority over Creek defaults.

        Args:
            fragment: The parsed fragment with metadata.

        Returns:
            A merged dict of frontmatter key-value pairs.
        """
        document_type = fragment.metadata.get("document_type", "notes")
        existing_fm = fragment.metadata.get("existing_frontmatter", {})
        platform = _infer_platform(document_type, Path(fragment.source_path))
        title = self._derive_title(fragment)

        creek_defaults: dict[str, Any] = {
            "type": "fragment",
            "title": title,
            "source": {
                "platform": platform,
                "original_file": fragment.source_path,
            },
            "created": fragment.timestamp.isoformat(),
        }

        return _merge_frontmatter(creek_defaults, existing_fm)

    def _derive_title(self, fragment: ParsedFragment) -> str:
        """Derive a title from the fragment content or source path.

        Looks for a level-1 heading (``# Title``) in the content. Falls
        back to the filename stem if no heading is found.

        Args:
            fragment: The parsed fragment.

        Returns:
            The derived title string.
        """
        heading_match = re.match(r"^#\s+(.+)$", fragment.content, re.MULTILINE)
        if heading_match:
            return heading_match.group(1).strip()
        return Path(fragment.source_path).stem
