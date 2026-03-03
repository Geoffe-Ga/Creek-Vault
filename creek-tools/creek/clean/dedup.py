"""Normalized deduplication — exact and content-hash-based duplicate detection.

Provides an in-memory deduplication registry that detects two kinds of
duplicates:

- **Exact match**: The fragment has the same deterministic SHA-256 hash
  derived from source, timestamp, and content.
- **Normalized match**: The fragment content, after stripping whitespace,
  lowercasing, and removing punctuation, hashes to the same value as a
  previously registered fragment.
"""

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from creek.ingest.base import generate_fragment_id


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

        Computes both the exact and normalized hashes. If a duplicate is
        found, returns the match information without re-registering. If
        the fragment is new, adds it to both indexes.

        Args:
            source: The source identifier (e.g., file path).
            timestamp: The fragment timestamp.
            content: The raw fragment text.

        Returns:
            A :class:`DeduplicationResult` indicating whether the fragment
            is a duplicate and, if so, what kind of match was found.
        """
        result = self._lookup(source, timestamp, content)
        if result.is_duplicate:
            return result

        exact_hash = _compute_exact_hash(source, timestamp, content)
        normalized_hash = _compute_normalized_hash(content)
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
        return self._lookup(source, timestamp, content)

    def clear(self) -> None:
        """Remove all registered fragments from the registry."""
        self._exact_index.clear()
        self._normalized_index.clear()

    def _lookup(
        self,
        source: str,
        timestamp: datetime,
        content: str,
    ) -> DeduplicationResult:
        """Perform the dual-hash lookup for duplicates.

        Checks the exact index first (higher specificity), then falls
        back to the normalized index.

        Args:
            source: The source identifier.
            timestamp: The fragment timestamp.
            content: The raw fragment text.

        Returns:
            A :class:`DeduplicationResult` with the match outcome.
        """
        exact_hash = _compute_exact_hash(source, timestamp, content)
        if exact_hash in self._exact_index:
            return DeduplicationResult(
                is_duplicate=True,
                match_type="exact",
                matched_fragment_id=self._exact_index[exact_hash],
            )

        normalized_hash = _compute_normalized_hash(content)
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
