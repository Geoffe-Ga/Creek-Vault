"""Consent management for first-time source processing.

Provides a consent workflow that gates pipeline processing on explicit
user approval. Before processing a data source for the first time, the
``ConsentManager`` displays file counts, content types, aggregate size,
and sample filenames, then requests confirmation.

Consent records are persisted as JSON in
``00-Creek-Meta/Processing-Log/consent-log.json``. The log is
append-only in *content* — a granted record is never removed or
rewritten — but not in *form*: every grant rewrites the whole file. That
rewrite therefore goes through :func:`creek._fsio.atomic_write_text`, so
an interrupted save cannot leave a half-file behind. A log that cannot
be parsed is quarantined beside itself rather than discarded, and a log
that cannot be read at all raises ``ConsentLogUnavailableError`` instead
of being silently reported as "no consent on record" (#1312).

Exports:
    ConsentManager: Orchestrates consent checking, prompting, and recording.
    ConsentRecord: Pydantic model for a single consent log entry.
    ConsentLog: Pydantic model wrapping the list of consent records.
    ConsentLogUnavailableError: Raised when the log cannot be read or written.
    SourceSummary: Pydantic model summarising a source directory's contents.
    _build_source_summary: Build a SourceSummary from a directory path.
    _matches_any_glob: Check if a filename matches any glob pattern.
    _quarantine_corrupt_log: Move an unparsable log aside under a unique name.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from creek._fsio import atomic_write_text
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


class ConsentLogUnavailableError(OSError):
    """Raised when the consent log cannot be read, written, or quarantined.

    Subclasses ``OSError`` rather than ``RuntimeError`` on purpose. The
    requirement is that an I/O failure touching the log surfaces as an
    I/O error instead of being flattened into "no consent recorded", and
    ``OSError`` is provably un-absorbed on this path: no ``except
    OSError`` handler sits between :class:`ConsentManager` and ``main``
    (the nearest ones guard unrelated pipeline and CLI work), whereas
    ``cli.py`` catches bare ``RuntimeError`` in four places — any of
    which would have swallowed this and handed the operator a generic
    message.

    The single-argument ``super().__init__`` is deliberate too.
    ``OSError`` parses an ``(errno, strerror)`` pair into an errno-typed
    subclass such as ``PermissionError``, but that parsing happens in
    ``OSError.__new__``, which CPython skips for a subclass overriding
    ``__init__``. Passing one message therefore leaves ``errno`` as
    ``None`` and keeps this class exactly what its name says; the
    original errno stays reachable on ``__cause__``.

    Attributes:
        path: The consent log file the failed operation was working on.
    """

    def __init__(self, path: Path, reason: object) -> None:
        """Initialise the error from the log path and the failure cause.

        Args:
            path: The consent log file that could not be used.
            reason: The underlying failure — normally the caught
                ``OSError`` or validation error — rendered into the
                message with ``str``.
        """
        super().__init__(f"consent log {path} is unavailable: {reason}")
        self.path = path


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
    files: list[Path] = [
        path
        for path in sorted(source_path.rglob("*"))
        if path.is_file() and not _matches_any_glob(path, exclusions)
    ]

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


def _quarantine_corrupt_log(path: Path) -> Path:
    """Move an unparsable consent log aside, preserving it byte for byte.

    The destination name is *reserved* with :func:`tempfile.mkstemp`
    rather than composed by hand, so two quarantines inside the same
    clock second cannot collide and an existing ``.corrupt-*`` sibling
    can never be overwritten. The timestamp in the prefix is there to
    orient the operator, not to provide uniqueness. The reservation is
    created empty and closed immediately; ``os.replace`` then overwrites
    it with the original.

    Because the reservation is made in *path*'s own directory the rename
    is same-filesystem, so ``EXDEV`` is unreachable and the inode simply
    moves. The quarantined file therefore carries the original's bytes
    *and* its mode, with no copy step that could itself be truncated.

    On failure the corrupt bytes are left exactly where they are — never
    discarded — and the empty reservation is removed so a failed
    quarantine leaves no litter in the log directory.

    Args:
        path: The consent log to move aside; it must exist.

    Returns:
        The path the log was moved to.

    Raises:
        ConsentLogUnavailableError: When the reservation or the rename
            fails. A read-only directory, for instance, fails in
            ``mkstemp`` before anything has moved. Wrapping both keeps
            every consent-log I/O failure inside one typed contract the
            CLI can report on, rather than letting a raw ``OSError``
            escape past the handler.
    """
    stamp = datetime.now(tz=LA_TZ).strftime("%Y%m%dT%H%M%S")
    # Load-bearing sentinel: it proves to the type checker that
    # ``reserved`` is bound at the ``return``, and it keeps the cleanup
    # from firing when ``mkstemp`` itself failed — an unconditional
    # unlink there would delete whatever the stale name last referred to.
    reserved = ""
    try:
        fd, reserved = tempfile.mkstemp(
            prefix=f"{path.name}.corrupt-{stamp}-", dir=path.parent
        )
        os.close(fd)
        os.replace(path, reserved)
    except OSError as exc:
        if reserved:
            with contextlib.suppress(OSError):
                Path(reserved).unlink()
        raise ConsentLogUnavailableError(path, exc) from exc
    return Path(reserved)


class ConsentManager:
    """Manage user consent for first-time source processing.

    Checks, prompts for, and records consent before the pipeline
    processes a new data source. Consent records are stored as JSON at
    ``log_dir/consent-log.json``: append-only in content — a granted
    record is never removed or rewritten — while each grant rewrites the
    whole file atomically. An unparsable log is quarantined rather than
    discarded, and an unreadable one raises
    :class:`ConsentLogUnavailableError` rather than reading as "no
    consent granted".

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

        Raises:
            ConsentLogUnavailableError: When the log exists but cannot
                be read, or an unparsable log cannot be quarantined.
                Deliberately not downgraded to ``False``: an unreadable
                log is not evidence that consent is absent, and
                answering ``False`` would prompt for a re-grant that
                then overwrites the very record it could not read.
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

        Loads the existing log, appends a ``ConsentRecord``, and
        rewrites the file atomically. Creates the log file if it does
        not exist.

        Args:
            source_type: The source type identifier.
            source_path: The filesystem path to the source.
            file_count: Number of files approved.
            exclusions: Glob patterns for excluded files.
            operator: Identity of the person granting consent.

        Raises:
            ConsentLogUnavailableError: When the existing log cannot be
                read or the updated log cannot be written. The new
                record is not persisted and the file on disk is left
                unchanged, so a failed grant is visible rather than
                being reported as recorded.
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

        A *missing* log is the only shape that yields an empty result: a
        first run has nothing recorded, so ``ConsentLog()`` is the
        truthful answer. Every other failure is reported rather than
        flattened, because "no record" and "could not read the record"
        drive opposite decisions — the first means *ask for consent*,
        and the second must never be allowed to impersonate it and then
        overwrite a real grant on the following save.

        There is no ``exists()`` pre-check. ``read_text`` raises
        ``FileNotFoundError`` for a missing file *and* for a missing
        parent directory, which is the same answer the guard gave, while
        closing the window in which the file disappears between the
        check and the read. The guard also sat outside the ``try``, so
        an unreadable parent made ``Path.exists`` raise
        ``PermissionError`` past a handler that was catching everything
        four lines below.

        An unparsable log is moved aside by
        :func:`_quarantine_corrupt_log` before a fresh one is started.
        That is a deliberate **write side effect of a read**, and the
        reason this method can raise on what looks like a read-only
        path; the alternative is the previous behaviour, which destroyed
        the evidence by overwriting it on the next grant.

        Two classifications are worth stating outright:

        - ``UnicodeDecodeError`` counts as corruption, not as a
          transient fault. Bad bytes on disk do not heal, and re-reading
          them can only reproduce the failure, so retrying would strand
          the operator with an unreadable log and no recovery path. It
          is a ``ValueError`` rather than an ``OSError``, so it falls
          past the propagating handler above it into the quarantine arm.
        - A zero-byte file is quarantined under the same uniform
          "unparsable, so quarantine" rule rather than being treated as
          a special "empty" case. Special-casing it would silently
          accept precisely the shape a torn ``write_text`` produces,
          which is the failure this method exists to stop hiding.
          ``'{}'`` is a different matter: it validates to an empty
          record list and is not quarantined.

        Returns:
            The loaded ``ConsentLog``, or an empty one when no log
            exists yet or the existing log was just quarantined.

        Raises:
            ConsentLogUnavailableError: When the log or its directory
                cannot be read — an unreadable file or an unreadable
                parent — or when quarantining an unparsable log fails.
        """
        try:
            text = self._log_file.read_text(encoding="utf-8")
            log = ConsentLog.model_validate_json(text)
        except FileNotFoundError:
            # Must precede the ``OSError`` arm below, which it subclasses.
            return ConsentLog()
        except OSError as exc:
            raise ConsentLogUnavailableError(self._log_file, exc) from exc
        except (UnicodeDecodeError, ValidationError) as exc:
            # ``model_validate_json`` raises pydantic's ``ValidationError``
            # for every malformed payload — truncated JSON included — so
            # there is no ``json.JSONDecodeError`` to catch here.
            quarantine = _quarantine_corrupt_log(self._log_file)
            logger.warning(
                "Consent log %s was unparsable (%s); the existing record has "
                "been quarantined to %s and a fresh log started — prior grants "
                "must be re-confirmed",
                self._log_file,
                exc,
                quarantine,
            )
            return ConsentLog()
        else:
            return log

    def _save_log(self, log: ConsentLog) -> None:
        """Save the consent log to disk, atomically.

        Creates the log directory if needed, then rewrites the whole
        file through :func:`creek._fsio.atomic_write_text`: staged in a
        tempfile and committed with ``os.replace``. The previous
        ``Path.write_text`` truncated the target before writing, so an
        interrupted save could manufacture exactly the corrupt log
        :meth:`_load_log` now has to recover from. With the atomic swap
        a reader sees either the old log or the new one — the same
        hardening merged for the vault index in #1307.

        Args:
            log: The consent log to persist.

        Raises:
            ConsentLogUnavailableError: When the directory cannot be
                created or the file cannot be written, so a failed save
                reaches the caller under the same typed contract as a
                failed load instead of tracebacking raw out of
                :meth:`record_consent`.
        """
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._log_file, log.model_dump_json(indent=2))
        except OSError as exc:
            raise ConsentLogUnavailableError(self._log_file, exc) from exc
