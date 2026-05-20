"""Google Drive pre-ingestion filter — skip duplicates, empty docs, flag multi-author.

Filters staged Google Drive files before they enter the ingest pipeline.
Detects five categories of problematic documents:

- **"Copy of..." duplicates**: Files with ``Copy of`` prefix or version
  suffixes like ``(1)``, ``(2)`` — preserves the newest version.
- **Empty documents**: Files with no extractable text or trivially
  short content.
- **Multi-author documents**: Files where collaborator contributions
  exceed a configurable threshold, flagged for review.
- **Stale documents**: Files not modified within a configurable
  timeframe, flagged for review.
- **Cross-format duplicates**: Identical content appearing in both
  ``.docx`` and ``.pdf`` formats — preserves the newest version.
"""

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class StagedFile(BaseModel):
    """Metadata for a staged Google Drive file awaiting ingestion.

    Attributes:
        path: Filesystem path to the staged file.
        filename: Original filename from Google Drive.
        content: Extracted text content (empty if not extractable).
        modified: Last modification timestamp.
        authors: List of contributor email addresses (may contain
            repeats to represent relative contribution weight).
        owner: Email address of the file owner.
        size_bytes: File size in bytes.
    """

    path: Path
    filename: str
    content: str
    modified: datetime
    authors: list[str] = Field(default_factory=list)
    owner: str = ""
    size_bytes: int = 0


class GoogleDriveFilterResult(BaseModel):
    """Result of filtering a single staged Google Drive file.

    Attributes:
        action: Recommended action: ``keep``, ``skip``, or ``flag``.
        reasons: Human-readable explanations for the action.
        duplicate_of: Path of the original file if this is a duplicate,
            or ``None`` if not a duplicate.
    """

    action: Literal["keep", "skip", "flag"]
    reasons: list[str]
    duplicate_of: str | None = None


# ---------------------------------------------------------------------------
# Filename patterns
# ---------------------------------------------------------------------------

_COPY_OF_PATTERN: re.Pattern[str] = re.compile(
    r"^copy of\s+",
    re.IGNORECASE,
)
"""Matches filenames starting with 'Copy of '."""

_VERSION_SUFFIX_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<base>.+?)\s*\((?P<version>\d+)\)(?P<ext>\.[^.]+)?$",
)
"""Matches filenames with version suffixes like ``report (1).docx``."""


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _strip_copy_of(filename: str) -> str | None:
    """Extract the original filename from a 'Copy of...' name.

    Args:
        filename: The filename to check.

    Returns:
        The original filename without the prefix, or ``None`` if
        the filename does not start with 'Copy of'.
    """
    match = _COPY_OF_PATTERN.match(filename)
    if match:
        return filename[match.end() :]
    return None


def _strip_version_suffix(filename: str) -> str | None:
    """Extract the base filename from a version-suffixed name.

    For example, ``report (1).docx`` returns ``report.docx``.

    Args:
        filename: The filename to check.

    Returns:
        The base filename without the version suffix, or ``None``
        if no version suffix is found.
    """
    match = _VERSION_SUFFIX_PATTERN.match(filename)
    if match:
        base = match.group("base").rstrip()
        ext = match.group("ext") or ""
        return f"{base}{ext}"
    return None


def _content_hash(content: str) -> str:
    """Compute a SHA-256 hash of normalized content for deduplication.

    Normalizes by stripping whitespace and lowercasing before hashing.

    Args:
        content: The text content to hash.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    normalized = " ".join(content.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _file_extension(filename: str) -> str:
    """Extract the lowercased file extension from a filename.

    Args:
        filename: The filename to extract the extension from.

    Returns:
        The extension including the dot (e.g. ``.docx``), or empty
        string if no extension.
    """
    return Path(filename).suffix.lower()


def _determine_action(
    index: int,
    skip_indices: set[int],
    flag_indices: set[int],
) -> Literal["keep", "skip", "flag"]:
    """Map file index to an action based on skip/flag sets.

    Args:
        index: The file index.
        skip_indices: Indices marked for skipping.
        flag_indices: Indices marked for flagging.

    Returns:
        The recommended action.
    """
    if index in skip_indices:
        return "skip"
    if index in flag_indices:
        return "flag"
    return "keep"


def _keep_newest_skip_rest(
    indices: list[int],
    files: list[StagedFile],
    results: list[dict[str, list[str] | str | None]],
    skip_indices: set[int],
    reason_template: str,
) -> None:
    """Keep the newest file in a group and skip the rest.

    Args:
        indices: File indices in the duplicate group.
        files: The full batch of staged files.
        results: Mutable results list to update.
        skip_indices: Set updated in-place with skipped indices.
        reason_template: Format string with ``{filename}`` placeholder.
    """
    newest_idx = max(
        indices,
        key=lambda idx: files[idx].modified,
    )
    newest_path = str(files[newest_idx].path)
    for idx in indices:
        if idx == newest_idx:
            continue
        reasons: list[str] = results[idx]["reasons"]  # type: ignore[assignment]
        reasons.append(
            reason_template.format(
                filename=files[newest_idx].filename,
            )
        )
        results[idx]["duplicate_of"] = newest_path
        skip_indices.add(idx)


# ---------------------------------------------------------------------------
# Internal result accumulator type
# ---------------------------------------------------------------------------

_FileResult = dict[str, list[str] | str | None]
"""Type alias for per-file mutable result dicts."""


# ---------------------------------------------------------------------------
# Filter class
# ---------------------------------------------------------------------------


class GoogleDriveFilter:
    """Pre-ingestion filter for Google Drive staged files.

    Evaluates staged files against configurable heuristics and returns
    a per-file action recommendation (keep, skip, or flag). Batch-level
    checks (duplicate detection, cross-format dedup) require all files
    to be passed together via :meth:`filter_batch`.

    All thresholds are configurable through constructor parameters.

    Attributes:
        staleness_days: Days without modification before flagging.
        multi_author_threshold: Non-owner contribution ratio (0.0--1.0)
            above which the file is flagged.
        min_content_length: Minimum character count after stripping
            whitespace for a file to not be considered empty.
        now: Reference timestamp for staleness calculations.
    """

    def __init__(
        self,
        *,
        now: datetime,
        staleness_days: int = 365,
        multi_author_threshold: float = 0.5,
        min_content_length: int = 10,
    ) -> None:
        """Initialise the filter with configurable thresholds.

        Args:
            now: Reference timestamp for staleness calculations.
            staleness_days: Days without modification before flagging
                as stale.
            multi_author_threshold: Non-owner contribution ratio
                (0.0--1.0) above which the file is flagged for review.
            min_content_length: Minimum stripped character count for
                a file to not be considered empty.
        """
        self.now = now
        self.staleness_days = staleness_days
        self.multi_author_threshold = multi_author_threshold
        self.min_content_length = min_content_length

    def filter_batch(
        self,
        files: list[StagedFile],
    ) -> list[GoogleDriveFilterResult]:
        """Filter a batch of staged files and return per-file results.

        Applies per-file checks (empty, staleness, multi-author) and
        batch-level checks (copy-of duplicates, version suffixes,
        cross-format deduplication). Results are returned in the same
        order as the input files.

        Args:
            files: List of staged files to evaluate.

        Returns:
            A list of :class:`GoogleDriveFilterResult`, one per input
            file, in the same order.
        """
        if not files:
            return []

        results: list[_FileResult] = [
            {"reasons": [], "duplicate_of": None} for _ in files
        ]
        skip_indices: set[int] = set()

        self._apply_empty_checks(files, results, skip_indices)
        self._apply_duplicate_checks(files, results, skip_indices)
        flag_indices = self._apply_flag_checks(
            files,
            results,
            skip_indices,
        )

        return self._build_results(
            files,
            results,
            skip_indices,
            flag_indices,
        )

    # ---- Orchestration helpers ----

    def _apply_empty_checks(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> None:
        """Mark empty/too-short files for skipping.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Set updated in-place.
        """
        for i, f in enumerate(files):
            if self._is_empty(f):
                reasons: list[str] = results[i]["reasons"]  # type: ignore[assignment]
                reasons.append("Empty or too-short document")
                skip_indices.add(i)

    def _apply_duplicate_checks(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> None:
        """Run all batch-level duplicate detection checks.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Set updated in-place.
        """
        self._check_copy_of_duplicates(files, results, skip_indices)
        self._check_version_duplicates(files, results, skip_indices)
        self._check_cross_format_duplicates(
            files,
            results,
            skip_indices,
        )

    def _apply_flag_checks(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> set[int]:
        """Apply staleness and multi-author checks to non-skipped files.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Indices already marked for skipping.

        Returns:
            Set of indices that should be flagged for review.
        """
        flag_indices: set[int] = set()
        for i, f in enumerate(files):
            if i in skip_indices:
                continue
            reasons: list[str] = results[i]["reasons"]  # type: ignore[assignment]
            self._check_staleness(f, reasons, flag_indices, i)
            self._check_multi_author(f, reasons, flag_indices, i)
        return flag_indices

    def _check_staleness(
        self,
        f: StagedFile,
        reasons: list[str],
        flag_indices: set[int],
        index: int,
    ) -> None:
        """Flag a file if it has not been modified recently.

        Args:
            f: The staged file to check.
            reasons: Mutable reasons list to append to.
            flag_indices: Set updated in-place.
            index: File index in the batch.
        """
        if self._is_stale(f):
            days = (self.now - f.modified).days
            reasons.append(
                f"Stale document: not modified in {days} days",
            )
            flag_indices.add(index)

    def _check_multi_author(
        self,
        f: StagedFile,
        reasons: list[str],
        flag_indices: set[int],
        index: int,
    ) -> None:
        """Flag a file if non-owner contributions exceed the threshold.

        Args:
            f: The staged file to check.
            reasons: Mutable reasons list to append to.
            flag_indices: Set updated in-place.
            index: File index in the batch.
        """
        if self._is_multi_author(f):
            reasons.append(
                "Multi-author document: significant non-owner contributions",
            )
            flag_indices.add(index)

    def _build_results(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
        flag_indices: set[int],
    ) -> list[GoogleDriveFilterResult]:
        """Convert internal result dicts to GoogleDriveFilterResult objects.

        Args:
            files: The full batch of staged files.
            results: Internal mutable results.
            skip_indices: Indices marked for skipping.
            flag_indices: Indices marked for flagging.

        Returns:
            A list of :class:`GoogleDriveFilterResult` in input order.
        """
        final: list[GoogleDriveFilterResult] = []
        for i in range(len(files)):
            reasons: list[str] = results[i]["reasons"]  # type: ignore[assignment]
            dup_of = results[i]["duplicate_of"]
            action = _determine_action(i, skip_indices, flag_indices)
            final.append(
                GoogleDriveFilterResult(
                    action=action,
                    reasons=reasons.copy(),
                    duplicate_of=str(dup_of) if dup_of else None,
                )
            )
        return final

    # ---- Per-file checks ----

    def _is_empty(self, f: StagedFile) -> bool:
        """Check whether a file has no meaningful content.

        Args:
            f: The staged file to check.

        Returns:
            ``True`` if the file content is empty or below the minimum
            length threshold.
        """
        stripped = f.content.strip()
        return len(stripped) < self.min_content_length

    def _is_stale(self, f: StagedFile) -> bool:
        """Check whether a file has not been modified recently.

        Args:
            f: The staged file to check.

        Returns:
            ``True`` if the file was last modified more than
            ``staleness_days`` ago.
        """
        cutoff = self.now - timedelta(days=self.staleness_days)
        return f.modified < cutoff

    def _is_multi_author(self, f: StagedFile) -> bool:
        """Check whether non-owner contributions exceed the threshold.

        Args:
            f: The staged file to check.

        Returns:
            ``True`` if non-owner contributions exceed the threshold.
        """
        if not f.authors:
            return False
        total = len(f.authors)
        non_owner = sum(1 for a in f.authors if a != f.owner)
        ratio = non_owner / total
        return ratio > self.multi_author_threshold

    # ---- Batch-level duplicate checks ----

    def _check_copy_of_duplicates(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> None:
        """Detect and mark 'Copy of...' filename duplicates.

        Files whose name starts with ``Copy of`` are marked for
        skipping. If the original file is present in the batch,
        the ``duplicate_of`` field references it.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Set updated in-place with skipped indices.
        """
        originals = self._build_filename_index(files)
        for i, f in enumerate(files):
            original_name = _strip_copy_of(f.filename)
            if original_name is None:
                continue
            reasons: list[str] = results[i]["reasons"]  # type: ignore[assignment]
            reasons.append(
                f"'Copy of' duplicate detected: {f.filename}",
            )
            skip_indices.add(i)
            self._link_to_original(
                original_name,
                originals,
                files,
                results,
                i,
            )

    @staticmethod
    def _build_filename_index(
        files: list[StagedFile],
    ) -> dict[str, int]:
        """Build a lookup of lowercased filenames to file indices.

        Args:
            files: The full batch of staged files.

        Returns:
            Mapping from lowercased filename to batch index.
        """
        return {f.filename.lower(): i for i, f in enumerate(files)}

    @staticmethod
    def _link_to_original(
        original_name: str,
        originals: dict[str, int],
        files: list[StagedFile],
        results: list[_FileResult],
        copy_idx: int,
    ) -> None:
        """Set duplicate_of if the original file is in the batch.

        Args:
            original_name: Expected original filename.
            originals: Filename-to-index lookup.
            files: The full batch of staged files.
            results: Mutable results list to update.
            copy_idx: Index of the copy file.
        """
        orig_key = original_name.lower()
        if orig_key in originals:
            orig_idx = originals[orig_key]
            results[copy_idx]["duplicate_of"] = str(
                files[orig_idx].path,
            )

    def _check_version_duplicates(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> None:
        """Detect and mark version-suffix duplicates.

        Groups files by their base name (without version suffix) and
        keeps only the newest version in each group.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Set updated in-place with skipped indices.
        """
        groups = self._build_version_groups(files, skip_indices)
        for indices in groups.values():
            if len(indices) < 2:
                continue
            _keep_newest_skip_rest(
                indices,
                files,
                results,
                skip_indices,
                "Version duplicate: older version of {filename}",
            )

    @staticmethod
    def _build_version_groups(
        files: list[StagedFile],
        skip_indices: set[int],
    ) -> dict[str, list[int]]:
        """Group files by base name (without version suffix).

        For each versioned file, also includes the non-suffixed
        original if it exists in the batch.

        Args:
            files: The full batch of staged files.
            skip_indices: Indices already marked for skipping.

        Returns:
            Mapping from lowercased base filename to list of indices.
        """
        groups: dict[str, list[int]] = {}
        for i, f in enumerate(files):
            if i in skip_indices:
                continue
            base = _strip_version_suffix(f.filename)
            if base is None:
                continue
            key = base.lower()
            if key not in groups:
                groups[key] = []
            groups[key].append(i)
            _include_original(files, skip_indices, key, groups[key])
        return groups

    def _check_cross_format_duplicates(
        self,
        files: list[StagedFile],
        results: list[_FileResult],
        skip_indices: set[int],
    ) -> None:
        """Detect identical content across different file formats.

        Groups non-skipped files by content hash. Within each group,
        if files have different extensions, the older files are marked
        as cross-format duplicates.

        Args:
            files: The full batch of staged files.
            results: Mutable results list to update.
            skip_indices: Set updated in-place with skipped indices.
        """
        hash_groups = _build_content_hash_groups(
            files,
            skip_indices,
        )
        for indices in hash_groups.values():
            if not _is_cross_format_group(indices, files):
                continue
            _keep_newest_skip_rest(
                indices,
                files,
                results,
                skip_indices,
                "Cross-format duplicate of {filename}",
            )


# ---------------------------------------------------------------------------
# Module-level helpers for batch checks
# ---------------------------------------------------------------------------


def _include_original(
    files: list[StagedFile],
    skip_indices: set[int],
    key: str,
    group: list[int],
) -> None:
    """Add the non-suffixed original file to a version group if present.

    Args:
        files: The full batch of staged files.
        skip_indices: Indices already marked for skipping.
        key: Lowercased base filename to search for.
        group: Mutable list of indices to append to.
    """
    for j, other in enumerate(files):
        if j in skip_indices:
            continue
        if other.filename.lower() == key and j not in group:
            group.append(j)


def _build_content_hash_groups(
    files: list[StagedFile],
    skip_indices: set[int],
) -> dict[str, list[int]]:
    """Group non-skipped files by content hash.

    Skips files with empty content after stripping whitespace.

    Args:
        files: The full batch of staged files.
        skip_indices: Indices already marked for skipping.

    Returns:
        Mapping from content hash to list of file indices.
    """
    hash_groups: dict[str, list[int]] = {}
    for i, f in enumerate(files):
        if i in skip_indices:
            continue
        stripped = f.content.strip()
        if not stripped:
            continue
        h = _content_hash(f.content)
        if h not in hash_groups:
            hash_groups[h] = []
        hash_groups[h].append(i)
    return hash_groups


def _is_cross_format_group(
    indices: list[int],
    files: list[StagedFile],
) -> bool:
    """Check whether a content-hash group spans multiple file formats.

    Args:
        indices: File indices in the group.
        files: The full batch of staged files.

    Returns:
        ``True`` if the group has at least 2 files with different
        extensions.
    """
    if len(indices) < 2:
        return False
    extensions = {_file_extension(files[idx].filename) for idx in indices}
    return len(extensions) >= 2
