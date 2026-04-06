"""Consent management for first-time source processing.

Provides a consent workflow that gates pipeline processing on explicit
user approval. Before processing a data source for the first time, the
``ConsentManager`` displays file counts, content types, aggregate size,
and sample filenames, then requests confirmation.

Consent records are persisted as append-only JSON in
``00-Creek-Meta/Processing-Log/consent-log.json``.

Exports:
    ConsentManager: Orchestrates consent checking, prompting, and recording.
    ConsentRecord: Pydantic model for a single consent log entry.
    ConsentLog: Pydantic model wrapping the list of consent records.
    SourceSummary: Pydantic model summarising a source directory's contents.
    _build_source_summary: Build a SourceSummary from a directory path.
    _matches_any_glob: Check if a filename matches any glob pattern.
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime
from pathlib import Path  # noqa: TC003 — needed at runtime by Pydantic

from pydantic import BaseModel, Field

from creek.ingest.base import LA_TZ

logger = logging.getLogger(__name__)

# Maximum number of sample filenames to display in consent prompt
_MAX_SAMPLE_FILENAMES = 10


class ConsentRecord(BaseModel):
    """A single consent record for a processed data source.

    Captures when, what, and who approved processing of a source.

    Attributes:
        timestamp: When consent was granted.
        source_type: The type of source (e.g. 'claude', 'chatgpt').
        source_path: Filesystem path to the source directory.
        file_count: Number of files approved for processing.
        exclusions: Glob patterns for excluded files.
        operator: Identity of the person who granted consent.
    """

    timestamp: datetime
    """When consent was granted."""

    source_type: str
    """The type of source (e.g. 'claude', 'chatgpt', 'generic')."""

    source_path: str
    """Filesystem path to the source directory."""

    file_count: int
    """Number of files approved for processing."""

    exclusions: list[str] = Field(default_factory=list)
    """Glob patterns for files excluded from processing."""

    operator: str
    """Identity of the person who granted consent."""


class ConsentLog(BaseModel):
    """Append-only log of consent records.

    Attributes:
        records: The ordered list of consent records.
    """

    records: list[ConsentRecord] = Field(default_factory=list)
    """The ordered list of consent records."""


class SourceSummary(BaseModel):
    """Summary of a source directory's contents for consent display.

    Attributes:
        file_count: Total number of files (after exclusions).
        total_size_bytes: Aggregate size of all files in bytes.
        content_types: Mapping of file extensions to their counts.
        sample_filenames: Up to 10 representative filenames.
    """

    file_count: int
    """Total number of files (after exclusions)."""

    total_size_bytes: int
    """Aggregate size of all files in bytes."""

    content_types: dict[str, int]
    """Mapping of file extensions to their counts."""

    sample_filenames: list[str]
    """Up to 10 representative filenames."""


def _matches_any_glob(file_path: Path, patterns: list[str]) -> bool:
    """Check if a filename matches any of the given glob patterns.

    Args:
        file_path: The file path to check (only the name is matched).
        patterns: A list of glob patterns (e.g. ``['*.log', '*.tmp']``).

    Returns:
        ``True`` if the filename matches any pattern.
    """
    name = file_path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _build_source_summary(source_path: Path, exclusions: list[str]) -> SourceSummary:
    """Build a summary of files in a source directory.

    Recursively walks ``source_path``, excluding files that match any
    pattern in ``exclusions``. Collects file counts by extension,
    total size, and a sample of filenames.

    Args:
        source_path: The directory to summarise.
        exclusions: Glob patterns for files to exclude.

    Returns:
        A ``SourceSummary`` with aggregate statistics.
    """
    files: list[Path] = []
    for path in sorted(source_path.rglob("*")):
        if path.is_file() and not _matches_any_glob(path, exclusions):
            files.append(path)

    total_size = sum(f.stat().st_size for f in files)

    content_types: dict[str, int] = {}
    for f in files:
        ext = f.suffix.lower() or "(no extension)"
        content_types[ext] = content_types.get(ext, 0) + 1

    sample_filenames = [f.name for f in files[:_MAX_SAMPLE_FILENAMES]]

    return SourceSummary(
        file_count=len(files),
        total_size_bytes=total_size,
        content_types=content_types,
        sample_filenames=sample_filenames,
    )


class ConsentManager:
    """Manage user consent for first-time source processing.

    Checks, prompts for, and records consent before the pipeline
    processes a new data source. Consent records are stored in an
    append-only JSON log at ``log_dir/consent-log.json``.

    Attributes:
        log_dir: Directory containing the consent log file.
    """

    def __init__(self, log_dir: Path) -> None:
        """Initialise the ConsentManager.

        Args:
            log_dir: Path to the consent log directory
                (e.g. ``00-Creek-Meta/Processing-Log/``).
        """
        self.log_dir = log_dir
        self._log_file = log_dir / "consent-log.json"

    def check_consent(self, source_type: str, source_path: str) -> bool:
        """Check whether consent has been previously recorded for a source.

        Args:
            source_type: The source type identifier (e.g. 'claude').
            source_path: The filesystem path to the source.

        Returns:
            ``True`` if a matching consent record exists.
        """
        log = self._load_log()
        return any(
            r.source_type == source_type and r.source_path == source_path
            for r in log.records
        )

    def record_consent(
        self,
        source_type: str,
        source_path: str,
        file_count: int,
        exclusions: list[str],
        operator: str,
    ) -> None:
        """Record a new consent entry in the log.

        Appends a ``ConsentRecord`` to the existing log file. Creates
        the log file if it does not exist.

        Args:
            source_type: The source type identifier.
            source_path: The filesystem path to the source.
            file_count: Number of files approved.
            exclusions: Glob patterns for excluded files.
            operator: Identity of the person granting consent.
        """
        log = self._load_log()
        record = ConsentRecord(
            timestamp=datetime.now(tz=LA_TZ),
            source_type=source_type,
            source_path=source_path,
            file_count=file_count,
            exclusions=exclusions,
            operator=operator,
        )
        log.records.append(record)
        self._save_log(log)
        logger.info(
            "Consent recorded for %s at %s (%d files)",
            source_type,
            source_path,
            file_count,
        )

    def get_source_summary(
        self, source_path: Path, exclusions: list[str]
    ) -> SourceSummary:
        """Build a summary of the source directory for consent display.

        Args:
            source_path: The directory to summarise.
            exclusions: Glob patterns for files to exclude.

        Returns:
            A ``SourceSummary`` with file counts, sizes, and samples.
        """
        return _build_source_summary(source_path, exclusions)

    def _load_log(self) -> ConsentLog:
        """Load the consent log from disk.

        Returns an empty log if the file does not exist or is invalid.

        Returns:
            The loaded ``ConsentLog``.
        """
        if not self._log_file.exists():
            return ConsentLog()

        try:
            text = self._log_file.read_text(encoding="utf-8")
            log = ConsentLog.model_validate_json(text)
        except Exception:
            logger.warning("Failed to parse consent log, starting fresh")
            return ConsentLog()
        else:
            return log

    def _save_log(self, log: ConsentLog) -> None:
        """Save the consent log to disk.

        Creates parent directories if needed.

        Args:
            log: The consent log to persist.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file.write_text(
            log.model_dump_json(indent=2),
            encoding="utf-8",
        )
