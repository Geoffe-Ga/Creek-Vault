"""Normalized deduplication — exact and content-hash-based duplicate detection.

Provides an in-memory deduplication registry that detects two kinds of
duplicates:

- **Exact match**: The fragment has the same deterministic SHA-256 hash
  derived from source, timestamp, and content.
- **Normalized match**: The fragment content, after stripping whitespace,
  lowercasing, and removing punctuation, hashes to the same value as a
  previously registered fragment.

Supports cross-run persistence via JSON index files and vault-seeding
from existing Obsidian fragment markdown files.
"""

import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from creek.ingest.base import generate_fragment_id

logger = logging.getLogger(__name__)


def _compute_exact_hash(source: str, timestamp: datetime, content: str) -> str:
    """Compute a deterministic SHA-256 hash from source, timestamp, and content.

    Uses the same hash-input format as
    :func:`creek.ingest.base.generate_fragment_id` to ensure consistency
    across the pipeline.

    Args:
        source: The source identifier (e.g., file path).
        timestamp: The fragment timestamp.
        content: The raw fragment text.

    Returns:
        Full hex-encoded SHA-256 digest string.
    """
    hash_input = f"{source}:{timestamp.isoformat()}:{content}"
    return hashlib.sha256(hash_input.encode()).hexdigest()


def _compute_normalized_hash(content: str) -> str:
    """Compute a SHA-256 hash of content after normalization.

    Normalization steps:
    1. Unicode NFC normalization
    2. Lowercase
    3. Remove all Unicode punctuation characters
    4. Collapse all whitespace to single spaces
    5. Strip leading/trailing whitespace

    Args:
        content: The raw fragment text.

    Returns:
        Full hex-encoded SHA-256 digest of the normalized content.
    """
    normalized = _normalize_content(content)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _normalize_content(content: str) -> str:
    """Normalize content for comparison.

    Applies NFC normalization, lowercasing, punctuation removal,
    and whitespace collapsing.

    Args:
        content: The raw text to normalize.

    Returns:
        The normalized text string.
    """
    text = unicodedata.normalize("NFC", content)
    text = text.lower()
    text = _strip_punctuation(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_punctuation(text: str) -> str:
    """Remove all Unicode punctuation characters from text.

    Uses the Unicode General Category to identify punctuation
    characters (categories starting with 'P').

    Args:
        text: Input string.

    Returns:
        String with all punctuation characters removed.
    """
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown text.

    Splits on ``---`` markers and parses the YAML block between them.
    Returns a tuple of (frontmatter_dict, body_content). If no valid
    frontmatter is found, returns an empty dict and the full text.

    Args:
        text: The raw markdown text (possibly with ``---`` delimiters).

    Returns:
        A tuple of ``(metadata_dict, body_text)``.
    """
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    yaml_block = parts[1]
    body = parts[2].strip()

    try:
        metadata = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}, text

    if not isinstance(metadata, dict):
        return {}, text

    return metadata, body


class DeduplicationResult(BaseModel):
    """Result of a deduplication check.

    Attributes:
        is_duplicate: Whether the fragment is a duplicate.
        match_type: The type of match found (``exact``, ``normalized``,
            or ``none``).
        matched_fragment_id: The ID of the matching fragment, or ``None``
            if no duplicate was found.
    """

    is_duplicate: bool
    match_type: Literal["exact", "normalized", "none"]
    matched_fragment_id: str | None = None


class Deduplicator:
    """In-memory deduplication registry for Creek fragments.

    Maintains two hash indexes:

    - **exact_index**: Maps exact SHA-256 hashes (from source + timestamp +
      content) to fragment IDs for exact duplicate detection.
    - **normalized_index**: Maps normalized content hashes to fragment IDs
      for content-similarity-based deduplication.

    Attributes:
        _exact_index: Mapping from exact hash to fragment ID.
        _normalized_index: Mapping from normalized hash to fragment ID.
    """

    def __init__(self) -> None:
        """Initialize the deduplicator with empty registries."""
        self._exact_index: dict[str, str] = {}
        self._normalized_index: dict[str, str] = {}

    @property
    def size(self) -> int:
        """Return the number of registered fragments.

        Returns:
            The count of unique exact hashes in the registry.
        """
        return len(self._exact_index)

    def register(
        self,
        source: str,
        timestamp: datetime,
        content: str,
    ) -> DeduplicationResult:
        """Check for duplicates and register the fragment if new.

        Computes both the exact and normalized hashes once. If a duplicate
        is found, returns the match information without re-registering. If
        the fragment is new, adds it to both indexes.

        Args:
            source: The source identifier (e.g., file path).
            timestamp: The fragment timestamp.
            content: The raw fragment text.

        Returns:
            A :class:`DeduplicationResult` indicating whether the fragment
            is a duplicate and, if so, what kind of match was found.
        """
        exact_hash = _compute_exact_hash(source, timestamp, content)
        normalized_hash = _compute_normalized_hash(content)

        result = self._check_hashes(exact_hash, normalized_hash)
        if result.is_duplicate:
            return result

        fragment_id = generate_fragment_id(source, timestamp, content)
        self._exact_index[exact_hash] = fragment_id
        self._normalized_index[normalized_hash] = fragment_id

        return result

    def check(
        self,
        source: str,
        timestamp: datetime,
        content: str,
    ) -> DeduplicationResult:
        """Check for duplicates without registering the fragment.

        Performs the same lookup as :meth:`register` but does not modify
        the registry.

        Args:
            source: The source identifier (e.g., file path).
            timestamp: The fragment timestamp.
            content: The raw fragment text.

        Returns:
            A :class:`DeduplicationResult` indicating whether the fragment
            would be a duplicate if registered.
        """
        exact_hash = _compute_exact_hash(source, timestamp, content)
        normalized_hash = _compute_normalized_hash(content)
        return self._check_hashes(exact_hash, normalized_hash)

    def clear(self) -> None:
        """Remove all registered fragments from the registry."""
        self._exact_index.clear()
        self._normalized_index.clear()

    def seed_from_vault(self, vault_path: Path) -> int:
        """Populate both hash indexes from existing vault fragment markdown files.

        Recursively scans the vault directory for ``.md`` files, parses YAML
        frontmatter to extract ``id``, ``source.original_file`` (or
        ``source.platform``), and ``created`` fields, then reads the body
        content. Each valid fragment is registered into both hash indexes.

        Files that lack valid frontmatter or required fields (``id``,
        ``source``, ``created``) are silently skipped.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.

        Returns:
            The number of fragments successfully seeded.
        """
        count = 0
        for md_file in sorted(vault_path.rglob("*.md")):
            if self._seed_from_file(md_file):
                count += 1
        return count

    def _seed_from_file(self, file_path: Path) -> bool:
        """Attempt to seed one markdown file into the indexes.

        Reads the file, parses frontmatter, extracts required fields, and
        populates both hash indexes if all fields are present.

        Args:
            file_path: Path to a markdown file to process.

        Returns:
            ``True`` if the fragment was successfully seeded, ``False``
            otherwise.
        """
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read file: %s", file_path)
            return False

        metadata, body = _parse_frontmatter(text)
        if not metadata:
            return False

        fragment_id = metadata.get("id")
        source_info = metadata.get("source")
        created = metadata.get("created")

        if not fragment_id or not source_info or not created:
            return False

        source = self._extract_source_key(source_info)
        if source is None:
            return False

        timestamp = self._parse_timestamp(created)
        if timestamp is None:
            return False

        exact_hash = _compute_exact_hash(source, timestamp, body)
        normalized_hash = _compute_normalized_hash(body)

        self._exact_index[exact_hash] = fragment_id
        self._normalized_index[normalized_hash] = fragment_id

        return True

    @staticmethod
    def _extract_source_key(source_info: Any) -> str | None:
        """Extract the source key string from a frontmatter source field.

        Handles both dict (``{original_file: ..., platform: ...}``)
        and plain string source values.

        Args:
            source_info: The ``source`` field from frontmatter metadata.

        Returns:
            The source key string, or ``None`` if not extractable.
        """
        if isinstance(source_info, dict):
            return (
                str(source_info.get("original_file") or source_info.get("platform", ""))
                or None
            )
        if isinstance(source_info, str):
            return source_info or None
        return None

    @staticmethod
    def _parse_timestamp(created: Any) -> datetime | None:
        """Parse a created timestamp from frontmatter into a datetime.

        Accepts ISO-format strings and ``datetime`` objects.

        Args:
            created: The ``created`` field from frontmatter metadata.

        Returns:
            A ``datetime`` object, or ``None`` if parsing fails.
        """
        if isinstance(created, datetime):
            return created
        if isinstance(created, str):
            try:
                return datetime.fromisoformat(created)
            except ValueError:
                logger.warning("Could not parse timestamp: %s", created)
                return None
        return None

    def save_index(self, path: Path) -> None:
        """Persist both hash indexes to a JSON file.

        Writes a JSON object with ``exact_index`` and ``normalized_index``
        keys, each mapping hash strings to fragment IDs.

        Args:
            path: Path to the JSON file to write. The file will be
                created or overwritten.
        """
        data = {
            "exact_index": dict(self._exact_index),
            "normalized_index": dict(self._normalized_index),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_index(self, path: Path) -> int:
        """Load hash indexes from a JSON file, merging with existing entries.

        Reads a JSON object with ``exact_index`` and ``normalized_index``
        keys and merges them into the current indexes. Existing entries
        are not overwritten.

        Args:
            path: Path to the JSON index file to read.

        Returns:
            The number of exact-index entries loaded (new entries only).

        Raises:
            FileNotFoundError: If the index file does not exist.
        """
        if not path.exists():
            msg = f"Index file not found: {path}"
            raise FileNotFoundError(msg)

        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)

        loaded_exact: dict[str, str] = data.get("exact_index", {})
        loaded_normalized: dict[str, str] = data.get("normalized_index", {})

        new_count = 0
        for hash_key, frag_id in loaded_exact.items():
            if hash_key not in self._exact_index:
                self._exact_index[hash_key] = frag_id
                new_count += 1

        for hash_key, frag_id in loaded_normalized.items():
            if hash_key not in self._normalized_index:
                self._normalized_index[hash_key] = frag_id

        return new_count

    def _check_hashes(
        self,
        exact_hash: str,
        normalized_hash: str,
    ) -> DeduplicationResult:
        """Check pre-computed hashes against the indexes.

        Checks the exact index first (higher specificity), then falls
        back to the normalized index.

        Args:
            exact_hash: Pre-computed exact SHA-256 hash.
            normalized_hash: Pre-computed normalized content hash.

        Returns:
            A :class:`DeduplicationResult` with the match outcome.
        """
        if exact_hash in self._exact_index:
            return DeduplicationResult(
                is_duplicate=True,
                match_type="exact",
                matched_fragment_id=self._exact_index[exact_hash],
            )

        if normalized_hash in self._normalized_index:
            return DeduplicationResult(
                is_duplicate=True,
                match_type="normalized",
                matched_fragment_id=self._normalized_index[normalized_hash],
            )

        return DeduplicationResult(
            is_duplicate=False,
            match_type="none",
            matched_fragment_id=None,
        )
