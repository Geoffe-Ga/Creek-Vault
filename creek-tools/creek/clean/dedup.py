"""Normalized deduplication — exact and content-hash-based duplicate detection.

Provides an in-memory deduplication registry that detects two kinds of
duplicates:

- **Exact match**: The fragment has the same deterministic SHA-256 hash
  derived from source, timestamp, and content.
- **Normalized match**: The fragment content, after stripping whitespace,
  lowercasing, and removing punctuation, hashes to the same value as a
  previously registered fragment.

Supports cross-run persistence via a JSON manifest file stored in the
vault at ``00-Creek-Meta/dedup-manifest.json``.
"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_MANIFEST_RELATIVE = Path("00-Creek-Meta") / "dedup-manifest.json"
"""Relative path from vault root to the deduplication manifest file."""

_MANIFEST_VERSION = 1
"""Current manifest schema version."""


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
    return re.sub(r"\s+", " ", text).strip()


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

        # Imported lazily to break the import cycle clean.dedup ->
        # ingest.base -> models -> classify -> generate -> clean.dedup:
        # a module-level import re-enters ingest.base before
        # generate_fragment_id is defined.
        from creek.ingest.base import generate_fragment_id

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

    def seed_from_vault(self, vault_path: Path) -> int:
        """Seed the deduplicator from a persisted manifest in the vault.

        Reads the manifest file at
        ``{vault_path}/00-Creek-Meta/dedup-manifest.json`` and merges
        its indexes into the current registry.

        Args:
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            The number of fragments seeded from the manifest.

        Raises:
            ValueError: If the manifest contains invalid JSON or an
                unsupported version.
        """
        manifest_path = vault_path / _MANIFEST_RELATIVE
        if not manifest_path.exists():
            return 0

        raw = manifest_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Corrupt dedup manifest at {manifest_path}: {exc}"
            raise ValueError(msg) from exc

        version = data.get("version")
        if version != _MANIFEST_VERSION:
            msg = (
                f"Unsupported manifest version {version} (expected {_MANIFEST_VERSION})"
            )
            raise ValueError(msg)

        exact: dict[str, str] = data.get("exact_index", {})
        normalized: dict[str, str] = data.get("normalized_index", {})

        self._exact_index.update(exact)
        self._normalized_index.update(normalized)

        return len(exact)

    def save_manifest(self, vault_path: Path) -> None:
        """Persist the current indexes to a manifest file in the vault.

        Writes both the exact and normalized indexes to
        ``{vault_path}/00-Creek-Meta/dedup-manifest.json``.

        Args:
            vault_path: Path to the root of the Obsidian vault.
        """
        manifest_path = vault_path / _MANIFEST_RELATIVE
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": _MANIFEST_VERSION,
            "exact_index": self._exact_index.copy(),
            "normalized_index": self._normalized_index.copy(),
        }
        manifest_path.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        """Remove all registered fragments from the registry."""
        self._exact_index.clear()
        self._normalized_index.clear()

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
