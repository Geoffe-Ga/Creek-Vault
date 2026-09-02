"""Redaction scanner — detect sensitive data in files without storing it.

The :class:`RedactionScanner` walks files line-by-line, matches against
both built-in and custom regex patterns, and returns :class:`RedactionMatch`
objects that contain only a salted SHA-256 hash of the matched text — never
the text itself.

A per-session random salt (16 bytes from :func:`os.urandom`) ensures that
hashes cannot be reversed via rainbow tables while still allowing
deduplication within a single scan session.

Enhanced features (Issue #14):
- Binary file detection via magic bytes
- File extension filtering
- Configurable directory exclusion patterns
- Progress bar via tqdm during directory scans
- Markdown summary grouped by file and severity
- Review queue with context for human review
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel
from tqdm import tqdm

from creek._containment import escaping_child, resolves_within
from creek.config import RedactionConfig  # noqa: TC001 — used at runtime
from creek.redact.patterns import (
    HIGH_ENTROPY_MIN_RUN,
    PATTERN_METADATA,
    REDACTION_PATTERNS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_CANONICAL_CONTAINMENT_PREDICATE: Final = resolves_within
"""Pins ``creek.redact.scanner.resolves_within`` to the canonical object.

Since #1498 this module's only containment call is :func:`escaping_child`,
which is itself defined in terms of :func:`resolves_within`. The name is kept
bound here anyway because #1294 pinned it BY IDENTITY —
``test_the_containment_predicate_has_exactly_one_definition`` asserts
``scanner.resolves_within is _containment.resolves_within`` — so dropping it
would retire the guard that stops a local re-implementation of "inside" from
appearing in this module. A binding, rather than a lint suppression, is what
keeps that contract honest.
"""

SYMLINK_SKIP_LABEL = "Files skipped (escaping symlink)"
"""Statistics label for files declined because they escape the scan root.

Exported rather than spelled twice: the markdown summary this module
renders and the Rich statistics table in
:func:`creek.redact.cli_commands._render_stats_table` describe the same
counter, and two literals are how the two surfaces drift apart.
"""

_EMPTY_SUMMARY_MARKDOWN = "# Redaction Scan Summary\n\nNo findings.\n"
"""Rendering for a scan that found nothing *and* declined nothing.

Held as a constant because it is a contract with every existing consumer of
a clean scan — notably CrawDad, which posts the markdown verbatim into a
Discord channel — and must stay byte-identical when nothing was skipped.
"""

_UNREAD_FILES_CAVEAT = (
    "No findings — but the scan declined to read one or more files, so this "
    "tree has not been fully checked. See the statistics above."
)
"""Replacement for a bare "No findings." when the walk skipped something.

A clean result reached by *not looking* reads identically to a clean tree,
which is the one place a silent skip in a safety scanner does real harm.
"""

FINDINGS_TABLE_HEADER = "| Line | Type | Severity |"
"""Column header of the per-file findings table in the markdown summary.

Exported rather than spelled twice: the table
:meth:`RedactionScanner.generate_markdown_summary` renders and the copy
of it quoted in ``docs/redaction.md`` are the same table, and two
literals are how the two surfaces drift apart.
"""

HIGH_ENTROPY_PATTERN_NAME = "high_entropy_string"
"""Pattern key used by the generic high-entropy detector."""

# Source of truth for the candidate regex lives on the PatternInfo entry
# so reports and the detector cannot drift apart silently.
HIGH_ENTROPY_CANDIDATE = PATTERN_METADATA[HIGH_ENTROPY_PATTERN_NAME].pattern
"""Substring shape for the generic-secret detector (≥20 base64url chars)."""

_LOG2_MIN_RUN: float = math.log2(HIGH_ENTROPY_MIN_RUN)
"""``log2`` of the sub-run window width, precomputed for the window scan.

Also the hard ceiling on any window's entropy: a window of
``HIGH_ENTROPY_MIN_RUN`` characters cannot carry more than
``log2(HIGH_ENTROPY_MIN_RUN)`` bits/char even when every character in it
is distinct.
"""

_COUNT_LOG_TABLE: tuple[float, ...] = tuple(
    count * math.log2(count) if count else 0.0
    for count in range(HIGH_ENTROPY_MIN_RUN + 1)
)
"""Lookup of ``c * log2(c)`` for every count a fixed-width window can hold.

A character's count inside a window of ``HIGH_ENTROPY_MIN_RUN`` characters
is bounded by the window width, so the table needs no more entries than
that. Index ``0`` is defined as ``0.0`` — the limit of ``c * log2(c)`` as
``c`` approaches zero — which lets the sliding window drop a character to
a zero count without a special case.
"""

_ENTROPY_FLOOR_BITS = 2.5
"""Lower bound (bits/char) for the entropy threshold at min_confidence=0.0.

Below this, even runs of ``aaaa…`` would slip through, so we do not
allow callers to relax the floor further.
"""

_ENTROPY_CEILING_BITS = 4.5
"""Upper bound (bits/char) for the entropy threshold at min_confidence=1.0.

Pure base64 random data tops out near 6 bits/char in theory, but
realistic short secret strings cluster between 3.5 and 4.5; setting
the ceiling at 4.5 keeps even hex-encoded 32-char secrets (which have
≈4 bits/char) catchable at the default ``0.6`` confidence.
"""


# Magic bytes for common binary file formats.
_BINARY_SIGNATURES: list[bytes] = [
    b"\x89PNG",  # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF8",  # GIF
    b"PK\x03\x04",  # ZIP / DOCX / XLSX
    b"PK\x05\x06",  # ZIP empty archive
    b"\x7fELF",  # ELF binary
    b"\xca\xfe\xba\xbe",  # Mach-O / Java class
    b"\xfe\xed\xfa",  # Mach-O
    b"\x00\x00\x01\x00",  # ICO
    b"%PDF",  # PDF
    b"\x1f\x8b",  # gzip
    b"BZ",  # bzip2
    b"\xfd7zXZ",  # xz
    b"Rar!",  # RAR
]

_BINARY_CHECK_BYTES: int = 8192


class RedactionMatch(BaseModel):
    """A single redaction finding — stores a salted hash, NOT the matched text.

    Attributes:
        file_path: Path to the file where the match was found.
        line_number: 1-based line number within the file.
        match_type: Name of the pattern that triggered the match.
        salted_hash: Hex-encoded SHA-256 hash of (salt + matched text).
    """

    file_path: Path
    line_number: int
    match_type: str
    salted_hash: str


@dataclass(frozen=True)
class ScanSummary:
    """Summary of a batch scan operation.

    Attributes:
        matches: All redaction matches found during the scan.
        files_scanned: Number of files that were scanned.
        files_skipped_binary: Number of files skipped as binary.
        files_skipped_extension: Number of files skipped due to extension.
        files_skipped_symlink: Number of symlinked children declined because
            their target resolves outside the scan root (#1087). Declared
            last so that any positional construction of the three original
            counters keeps its meaning.
    """

    matches: list[RedactionMatch] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped_binary: int = 0
    files_skipped_extension: int = 0
    files_skipped_symlink: int = 0


class RedactionScanner:
    """Scan files for sensitive data using compiled regex patterns.

    Each scanner instance generates a unique random salt so that matched
    text can be hashed for deduplication without ever being stored.

    Args:
        config: A :class:`RedactionConfig` controlling which patterns to
            apply and which strings to allowlist.
    """

    def __init__(self, config: RedactionConfig) -> None:
        """Initialise the scanner with configuration and a fresh session salt.

        Args:
            config: Redaction configuration (patterns, allowlist, etc.).
        """
        self.config = config
        self.salt: bytes = os.urandom(16)
        self._patterns = self._build_patterns()
        self._marker_runs = emitted_marker_runs(
            config.replacement_template,
            frozenset(self._patterns) | {HIGH_ENTROPY_PATTERN_NAME},
        )

    def _build_patterns(self) -> dict[str, re.Pattern[str]]:
        """Merge built-in patterns with any custom patterns from config.

        Returns:
            Combined dictionary of pattern name to compiled regex.
        """
        patterns: dict[str, re.Pattern[str]] = REDACTION_PATTERNS.copy()
        for name, raw in self.config.custom_patterns.items():
            patterns[name] = re.compile(raw)
        return patterns

    def _hash_match(self, text: str) -> str:
        """Compute a salted SHA-256 hash of *text*.

        Args:
            text: The sensitive string to hash.

        Returns:
            Hex-encoded SHA-256 digest of ``salt + text.encode()``.
        """
        return hashlib.sha256(self.salt + text.encode()).hexdigest()  # nosec B324

    def _is_allowlisted(self, text: str) -> bool:
        """Check whether *text* appears in the false-positive allowlist.

        Args:
            text: The matched string to check.

        Returns:
            ``True`` if the string should be excluded from results.
        """
        return text in self.config.false_positive_allowlist

    @staticmethod
    def is_binary(file_path: Path) -> bool:
        """Detect whether a file is binary using magic bytes and null checks.

        Reads the first 8192 bytes and checks for known binary file
        signatures and null bytes.

        Args:
            file_path: Path to the file to check.

        Returns:
            ``True`` if the file appears to be binary.
        """
        try:
            chunk = file_path.read_bytes()[:_BINARY_CHECK_BYTES]
        except OSError:
            return False

        if not chunk:
            return False

        for sig in _BINARY_SIGNATURES:
            if chunk.startswith(sig):
                return True

        return b"\x00" in chunk

    def _has_supported_extension(self, file_path: Path) -> bool:
        """Check whether the file has a supported extension.

        Args:
            file_path: Path to the file to check.

        Returns:
            ``True`` if the file extension is in the supported list.
        """
        return file_path.suffix.lower() in self.config.supported_extensions

    def _is_excluded(self, file_path: Path) -> bool:
        """Check whether any part of the file path matches an exclusion pattern.

        Args:
            file_path: Path to the file to check.

        Returns:
            ``True`` if the file should be excluded from scanning.
        """
        parts = file_path.parts
        return any(excl in parts for excl in self.config.exclude_patterns)

    def scan_file(self, file_path: Path) -> list[RedactionMatch]:
        """Scan a single file for sensitive data patterns.

        Reads the file line-by-line and returns a :class:`RedactionMatch`
        for every pattern hit that is not on the false-positive allowlist.

        Pattern-specific post-validators are applied after a regex hit:
        the ``credit_card`` pattern is filtered through the Luhn checksum
        so that 16-digit identifiers that happen to match a BIN range
        are not reported as cards.

        Args:
            file_path: Path to the file to scan.

        Returns:
            List of :class:`RedactionMatch` objects (may be empty).

        Raises:
            FileNotFoundError: If *file_path* does not exist.
        """
        if not file_path.exists():
            msg = f"File not found: {file_path}"
            raise FileNotFoundError(msg)

        matches: list[RedactionMatch] = []
        text = file_path.read_text(encoding="utf-8", errors="replace")

        for line_num, line in enumerate(text.splitlines(), start=1):
            for name, pattern in self._patterns.items():
                for m in pattern.finditer(line):
                    matched_text = m.group()
                    if self._is_allowlisted(matched_text):
                        continue
                    if not post_validate(name, matched_text):
                        continue
                    matches.append(
                        RedactionMatch(
                            file_path=file_path,
                            line_number=line_num,
                            match_type=name,
                            salted_hash=self._hash_match(matched_text),
                        )
                    )
            matches.extend(self._scan_high_entropy(file_path, line_num, line))

        return matches

    def _scan_high_entropy(
        self,
        file_path: Path,
        line_num: int,
        line: str,
    ) -> list[RedactionMatch]:
        """Flag base64url-ish substrings whose Shannon entropy clears the bar.

        The threshold is derived from :pyattr:`RedactionConfig.min_confidence`
        so users can tune false-positive sensitivity without editing code:
        ``0.0`` flags any ≥20-char base64url-ish run, ``1.0`` requires the
        substring to be effectively random.

        Gating is delegated to :func:`has_high_entropy_region`, so a run
        counts as high-entropy when the whole run clears the threshold
        **or** when any contiguous ``HIGH_ENTROPY_MIN_RUN``-character
        window of it does. Averaging over a whole run alone let a
        predictable neighbour mask a genuine secret (Issue #942). What
        gets reported is still the whole candidate run, never the hot
        sub-window: the match is hashed for deduplication, and hashing a
        fragment would neither dedupe against nor describe the span that
        ``--apply`` removes.

        Candidacy comes from :func:`iter_unmarked_candidates`, the one
        definition this method shares with the redactor's two sweeps, so
        a run belonging to a marker *this scanner's own configuration*
        renders is never reported (#945). Without that, ``--scan``,
        ``--report``, ``--review``, the MCP scan tool and the fail-loud
        ``creek process`` redaction stage all raised a phantom
        ``high_entropy_string`` finding on an already-redacted tree,
        which no amount of ``--apply`` could clear. The carve-out is
        name-keyed rather than shape-keyed — see
        :func:`iter_unmarked_candidates` for the bound and the two
        deliberate non-goals.

        Args:
            file_path: Path to the file being scanned (passed through to
                the resulting :class:`RedactionMatch`).
            line_num: 1-based line number for the match.
            line: The single line to scan.

        Returns:
            One :class:`RedactionMatch` per high-entropy substring found.
        """
        threshold = entropy_threshold(self.config.min_confidence)
        results: list[RedactionMatch] = []
        for candidate in iter_unmarked_candidates(line, marker_runs=self._marker_runs):
            text = candidate.group()
            if self._is_allowlisted(text):
                continue
            if not has_high_entropy_region(text, threshold):
                continue
            results.append(
                RedactionMatch(
                    file_path=file_path,
                    line_number=line_num,
                    match_type=HIGH_ENTROPY_PATTERN_NAME,
                    salted_hash=self._hash_match(text),
                )
            )
        return results

    def scan_batch(
        self,
        dir_path: Path,
        *,
        progress: bool = False,
    ) -> ScanSummary:
        """Recursively scan a directory and return a full scan summary.

        Returns a :class:`ScanSummary` with match details and statistics
        about files scanned, skipped (binary), skipped (extension), and
        skipped as symlinks escaping *dir_path*.

        This is the single chokepoint every read-path caller reaches —
        ``creek redact --scan/--apply/--review`` via
        :func:`creek.redact.cli_commands._scan_source`, ``creek.redact.scan``
        over MCP, and ``creek process`` via
        :meth:`Pipeline._run_redaction <creek.pipeline.Pipeline>` — so the
        containment policy lives here in :func:`_scannable_candidates` and
        not in any one of them (#1087).

        Args:
            dir_path: Path to the directory to scan.
            progress: If ``True``, display a tqdm progress bar.

        Returns:
            A :class:`ScanSummary` with all results and statistics.

        Raises:
            FileNotFoundError: If *dir_path* does not exist.
        """
        if not dir_path.exists():
            msg = f"Directory not found: {dir_path}"
            raise FileNotFoundError(msg)

        candidates, files_skipped_symlink = _scannable_candidates(dir_path)

        matches: list[RedactionMatch] = []
        files_scanned = 0
        files_skipped_binary = 0
        files_skipped_extension = 0

        file_iter: tqdm[Path] | list[Path]
        if progress:
            file_iter = tqdm(
                candidates,
                desc="Scanning files",
                unit="file",
            )
        else:
            file_iter = candidates

        for child in file_iter:
            if self._is_excluded(child):
                continue

            if not self._has_supported_extension(child):
                files_skipped_extension += 1
                continue

            if self.is_binary(child):
                files_skipped_binary += 1
                continue

            matches.extend(self.scan_file(child))
            files_scanned += 1

        return ScanSummary(
            matches=matches,
            files_scanned=files_scanned,
            files_skipped_binary=files_skipped_binary,
            files_skipped_extension=files_skipped_extension,
            files_skipped_symlink=files_skipped_symlink,
        )

    @staticmethod
    def extract_context(
        file_path: Path,
        line_number: int,
        window: int = 2,
    ) -> list[str]:
        """Extract context lines around a match location.

        Reads the file and returns lines from ``line_number - window``
        to ``line_number + window`` (1-based, clamped to file bounds).

        Args:
            file_path: Path to the source file.
            line_number: 1-based line number of the match.
            window: Number of lines before and after to include.

        Returns:
            List of context lines (may be fewer than ``2 * window + 1``
            at file boundaries).
        """
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        all_lines = text.splitlines()
        start = max(0, line_number - 1 - window)
        end = min(len(all_lines), line_number + window)
        return all_lines[start:end]

    def generate_markdown_summary(self, summary: ScanSummary) -> str:
        """Generate a human-readable markdown summary of scan results.

        Results are grouped by file and sorted by severity within
        each file section.

        The findings-free arm is delegated to :func:`_no_findings_markdown`,
        which distinguishes "nothing to report" from "nothing was read".

        Args:
            summary: The scan summary to format.

        Returns:
            Markdown-formatted string.
        """
        if not summary.matches:
            return _no_findings_markdown(summary)

        lines: list[str] = [
            "# Redaction Scan Summary",
            "",
            "## Statistics",
            "",
            *_statistics_lines(summary),
            "",
        ]

        # Severity breakdown.
        by_sev = _count_by_severity(summary.matches)
        if by_sev:
            lines.extend(("### By Severity", ""))
            for sev in ("critical", "high", "medium", "low"):
                if sev in by_sev:
                    lines.append(f"- **{sev}**: {by_sev[sev]}")
            lines.append("")

        # Group by file.
        by_file: dict[str, list[RedactionMatch]] = defaultdict(list)
        for match in summary.matches:
            by_file[str(match.file_path)].append(match)

        lines.extend(("## Findings by File", ""))

        for file_key in sorted(by_file):
            file_matches = sorted(
                by_file[file_key],
                key=lambda m: _severity_rank(_get_severity(m.match_type)),
            )
            lines.extend(
                (
                    f"### `{file_key}`",
                    "",
                    FINDINGS_TABLE_HEADER,
                    "|------|------|----------|",
                )
            )
            for fm in file_matches:
                sev = _get_severity(fm.match_type)
                lines.append(f"| {fm.line_number} | {fm.match_type} | {sev} |")
            lines.append("")

        return "\n".join(lines)

    def generate_review_queue(self, summary: ScanSummary) -> str:
        """Generate a markdown review queue with context and checkboxes.

        Each finding includes surrounding context lines, pattern type,
        severity, and checkboxes for the human reviewer to mark as
        ``confirmed sensitive`` or ``false positive``.

        Args:
            summary: The scan summary to format.

        Returns:
            Markdown-formatted review queue string.
        """
        if not summary.matches:
            return "# Redaction Review Queue\n\nNo findings to review.\n"

        lines: list[str] = [
            "# Redaction Review Queue",
            "",
            (
                "Review each finding below. Check the appropriate box "
                "to classify each match."
            ),
            "",
        ]

        # Group by file.
        by_file: dict[str, list[RedactionMatch]] = defaultdict(list)
        for match in summary.matches:
            by_file[str(match.file_path)].append(match)

        finding_num = 0
        for file_key in sorted(by_file):
            file_matches = sorted(
                by_file[file_key],
                key=lambda m: m.line_number,
            )
            lines.extend((f"## `{file_key}`", ""))

            for fm in file_matches:
                finding_num += 1
                severity = _get_severity(fm.match_type)
                context = self.extract_context(fm.file_path, fm.line_number)

                lines.extend(
                    (
                        f"### Finding {finding_num}",
                        "",
                        f"- **Type**: {fm.match_type}",
                        f"- **Severity**: {severity}",
                        f"- **Line**: {fm.line_number}",
                        "",
                    )
                )

                if context:
                    lines.append("```")
                    lines.extend(context)
                    lines.extend(("```", ""))

                lines.extend(
                    (
                        f"- [ ] Confirmed sensitive (finding {finding_num})",
                        f"- [ ] False positive (finding {finding_num})",
                        "",
                    )
                )

        return "\n".join(lines)


def _no_findings_markdown(summary: ScanSummary) -> str:
    """Render the markdown for a scan that produced no matches.

    Two different documents, because "we read everything and found nothing"
    and "we found nothing in what we agreed to read" are two different
    facts, and only the first one licenses the reader's conclusion that the
    tree is clean. A scan that declined a file therefore gets the full
    statistics block — which names the skip — plus
    :data:`_UNREAD_FILES_CAVEAT` in place of a bare "No findings.".

    A scan that declined nothing renders :data:`_EMPTY_SUMMARY_MARKDOWN`
    byte-for-byte, so the new wording is not paid for out of the pocket of
    every existing consumer of a clean scan.

    Args:
        summary: A scan summary whose ``matches`` list is empty.

    Returns:
        Markdown-formatted string.
    """
    if not summary.files_skipped_symlink:
        return _EMPTY_SUMMARY_MARKDOWN

    return "\n".join(
        (
            "# Redaction Scan Summary",
            "",
            "## Statistics",
            "",
            *_statistics_lines(summary),
            "",
            _UNREAD_FILES_CAVEAT,
            "",
        )
    )


def _statistics_lines(summary: ScanSummary) -> list[str]:
    """Render the bullet list of the Statistics block.

    The escaping-symlink row is emitted only when the counter is non-zero.
    A permanent "escaping symlink: 0" line in front of every operator whose
    tree has never held one is the fastest way to teach a reader to skip
    that part of the report.

    Args:
        summary: The scan summary to describe.

    Returns:
        One markdown bullet per reported counter, in display order.
    """
    lines = [
        f"- **Files scanned**: {summary.files_scanned}",
        f"- **Files skipped (binary)**: {summary.files_skipped_binary}",
        f"- **Files skipped (extension)**: {summary.files_skipped_extension}",
    ]
    if summary.files_skipped_symlink:
        lines.append(f"- **{SYMLINK_SKIP_LABEL}**: {summary.files_skipped_symlink}")
    lines.append(f"- **Total findings**: {len(summary.matches)}")
    return lines


# ``resolves_within`` is re-exported, not defined here. It began as this
# module's private ``_resolves_within``, was made public by #1293 when the
# redaction CLI needed the same predicate for a directly-named path, and
# moved to :mod:`creek._containment` in #1294 when the ingestor discovery
# gate became its third consumer. Three copies of a containment predicate
# are three predicates that drift; one definition cannot. The name stays
# bound here because :mod:`creek.redact.cli_commands` imports it from this
# module and ``_scannable_candidates`` below calls it.
#
# The dependency direction is deliberate: ``creek._containment`` is
# stdlib-only and must not import this module back, because
# :mod:`creek.ingest.base` depends on it and compiling this module's regex
# battery on every ingest would be the cost of that cycle.


def _scannable_candidates(dir_path: Path) -> tuple[list[Path], int]:
    """Return the files under *dir_path* the scanner may read, plus escapes.

    The only place :mod:`creek.redact.scanner` enumerates the filesystem, so
    the containment policy cannot be applied to some children and not others.
    That "one walk, one gate" property is itself pinned, by
    ``test_scan_batch_is_the_only_filesystem_walk_in_the_scanner_module``.

    The admission policy is the one the shipped SEC-003 *write* guard already
    uses — :func:`creek.redact.cli_commands._assert_no_escaping_symlinks` —
    rather than a second, subtly different predicate: resolve the root
    once, and for a child that is *itself* a symlink require its resolved
    target to be a descendant of that root. Non-symlink children are never
    resolved, which is what keeps a root reached *through* a symlinked
    component scannable (``/tmp`` → ``/private/tmp`` on macOS); resolving
    them too would flag every child of such a root as escaping. Resolving
    the root and ``lstat``-checking only the leaf is what makes a per-leaf
    check sound: ``Path.rglob`` surfaces a symlinked *directory* as an entry
    but does not descend into it, and ``is_file()`` then drops it, so the
    residual is bounded to symlinked files.

    Skips are logged naming only the as-scanned path. The resolved target is
    never named: disclosing it is the very oracle #1087 closes.

    Enumerates with ``os.walk(..., onerror=...)`` rather than ``rglob`` since
    #1498. ``rglob`` swallows a failed ``scandir``, so a directory the scanner
    could not list was indistinguishable from an empty one: the subtree was
    not scanned, not counted in ``escaped``, and not reported, and the
    operator read a clean result over a region that was never opened. The
    ruling for this surface is REPORT, not refuse — ``--scan`` writes nothing
    and :meth:`creek.pipeline.Pipeline._run_redaction` reaches it on the
    unattended ``creek process`` / ``creek sync`` path, so a refusal here would
    disable the operator's whole safety scan over one chmod-000 directory.
    The count is deliberately NOT folded into ``escaped``: an unreadable
    directory is not an escaping symlink, and conflating them would make the
    one statistic the pipeline refusal is built on mean two things.

    The ``os.walk`` swap is bound to be parity-preserving with the ``rglob``
    it replaces, which is why it iterates ``filenames`` and re-applies
    ``is_file()``: ``os.walk`` classifies a symlink-to-directory into
    ``dirnames`` (and, with ``followlinks=False``, does not descend), exactly
    as ``rglob`` yielded it and ``is_file()`` dropped it, while a *broken*
    symlink lands in ``filenames`` and is dropped by ``is_file()`` before the
    containment test — as it was before. The same idiom is proven in
    :func:`creek.ingest.markdown._enumerate_markdown_paths`.

    Known gaps, deliberately out of scope here:

    * Escaping ANCESTOR components. The directly-named path is now tested
      for containment by :mod:`creek.redact.cli_commands` before any mode
      reads through it (#1293), but a path reached *through* a link one
      level up — ``<root>/linkdir/a.md``, where ``linkdir`` is the escaping
      link — is still admitted here. That is not an oversight: it is the
      resolve-the-root / ``lstat``-the-leaf policy documented above, applied
      consistently on both surfaces.
    Closed since this docstring was written: #1294 — the ingestor walks no
    longer follow symlinks out of the source root.
    :func:`creek._containment.assert_source_contained` gates
    ``Ingestor.ingest`` for every registered ingestor, so the pipeline
    refusal built on this counter is now a second line of defence rather
    than the only one.

    Args:
        dir_path: The scan root, exactly as the caller supplied it.

    Returns:
        ``(candidates, escaped)`` — the admitted files, sorted, and the
        number of symlinked children declined for resolving outside the root.
    """
    resolved_root = dir_path.resolve(strict=False)
    candidates: list[Path] = []
    escaped = 0

    def _record(error: OSError) -> None:
        """Report a directory ``scandir`` refused instead of discarding it.

        Args:
            error: The ``OSError`` ``os.walk`` would otherwise swallow. Only
                its ``filename`` — the directory, as walked — is logged.
        """
        logger.warning(
            "Skipping a subtree the scan could not list: %s. Nothing beneath "
            "it was scanned; restore read+execute permission to cover it.",
            error.filename,
        )

    for dirpath, _dirnames, filenames in os.walk(
        dir_path,
        onerror=_record,
        followlinks=False,
    ):
        for name in filenames:
            child = Path(dirpath) / name
            if not child.is_file():
                continue
            if escaping_child(child, resolved_root):
                logger.warning("Skipping symlink that escapes the scan root: %s", child)
                escaped += 1
                continue
            candidates.append(child)

    return sorted(candidates), escaped


def _entropy_from_counts(counts: dict[str, int], length: int) -> float:
    """Return the Shannon entropy of a string given its character counts.

    Args:
        counts: Character frequencies of a non-empty string.
        length: Total number of characters the counts were taken over.

    Returns:
        Entropy in bits per character.
    """
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def shannon_entropy(text: str) -> float:
    """Return the Shannon entropy (bits/char) of *text*.

    Args:
        text: The string to measure.

    Returns:
        Entropy in bits per character; ``0.0`` for the empty string.
    """
    if not text:
        return 0.0
    return _entropy_from_counts(Counter(text), len(text))


def has_high_entropy_region(text: str, threshold: float) -> bool:
    """Report whether *text* carries a dense enough region to be a secret.

    The single decision point shared by ``--scan``
    (:meth:`RedactionScanner._scan_high_entropy`) and ``--apply``
    (:meth:`creek.redact.redactor.Redactor._collect_high_entropy_spans`),
    so the two cannot drift apart.

    A run qualifies when its *whole-run* Shannon entropy reaches
    *threshold*, or when any contiguous window of exactly
    :data:`~creek.redact.patterns.HIGH_ENTROPY_MIN_RUN` characters does.
    The window gate exists because concatenation averages: gluing
    predictable filler to a genuine secret drags the whole-run average
    below the threshold and used to hide the secret from both call sites
    (Issue #942). The window width equals the candidate regex's own
    repetition floor, so the invariant restored is precisely "a run the
    detector would flag standing alone stays flagged when concatenated to
    a neighbour".

    Args:
        text: The candidate run to measure.
        threshold: Entropy bar in bits/char, from
            :func:`entropy_threshold`. A value landing exactly on the bar
            counts as clearing it.

    Returns:
        ``True`` when the whole run or any window of it reaches
        *threshold*; ``False`` for the empty string.
    """
    if not text:
        return False

    counts = Counter(text)
    if _entropy_from_counts(counts, len(text)) >= threshold:
        return True

    # At or below the window width the only window is the whole run,
    # which the check above already decided.
    if len(text) <= HIGH_ENTROPY_MIN_RUN:
        return False

    # Entropy is bounded above by log2(distinct symbols), and a window can
    # hold neither more symbols than the run has nor more than its own
    # width. When even that ceiling falls short, no window can reach the
    # bar. The comparison is strict: a perfectly uniform window *attains*
    # the ceiling, so an exact tie must still be scanned. This is also why
    # the gate is provably inert at ``min_confidence=1.0``, where the
    # threshold (4.5) exceeds log2(HIGH_ENTROPY_MIN_RUN).
    if math.log2(min(len(counts), HIGH_ENTROPY_MIN_RUN)) < threshold:
        return False

    return _window_entropy_clears(text, threshold)


def _window_entropy_clears(text: str, threshold: float) -> bool:
    """Slide a fixed-width window over *text*, testing entropy at each step.

    Each step costs O(1) rather than O(window width): for a window of
    fixed width ``L`` the entropy is exactly ``log2(L) - acc / L`` where
    ``acc`` is ``sum(c * log2(c))`` over the window's character counts, so
    only the outgoing and incoming characters' contributions need
    adjusting. Re-measuring each window from scratch instead is roughly
    48x slower on a token-dense body.

    Args:
        text: A run strictly longer than
            :data:`~creek.redact.patterns.HIGH_ENTROPY_MIN_RUN`.
        threshold: Entropy bar in bits/char; an exact tie clears it.

    Returns:
        ``True`` when any window reaches *threshold*.
    """
    width = HIGH_ENTROPY_MIN_RUN
    counts: Counter[str] = Counter(text[:width])
    acc = sum(_COUNT_LOG_TABLE[count] for count in counts.values())
    if _LOG2_MIN_RUN - acc / width >= threshold:
        return True

    for index in range(width, len(text)):
        outgoing = text[index - width]
        leaving = counts[outgoing]
        acc += _COUNT_LOG_TABLE[leaving - 1] - _COUNT_LOG_TABLE[leaving]
        counts[outgoing] = leaving - 1

        incoming = text[index]
        arriving = counts[incoming]
        acc += _COUNT_LOG_TABLE[arriving + 1] - _COUNT_LOG_TABLE[arriving]
        counts[incoming] = arriving + 1

        if _LOG2_MIN_RUN - acc / width >= threshold:
            return True

    return False


MarkerRuns = dict[str, tuple[tuple[str, int], ...]]
"""Candidate runs found inside the markers one config would render.

Maps a run's exact text to every ``(marker literal, offset of the run
within that marker)`` pair that produces it. Built once per scanner or
redactor instance by :func:`emitted_marker_runs`; consumed by
:func:`iter_unmarked_candidates`.
"""


def iter_unmarked_candidates(
    text: str,
    *,
    marker_runs: MarkerRuns,
) -> Iterator[re.Match[str]]:
    """Yield the high-entropy candidate runs in *text* that are not our own.

    The single definition of "is this run a candidate", shared by
    ``--scan`` (:meth:`RedactionScanner._scan_high_entropy`), ``--apply``
    (:meth:`creek.redact.redactor.Redactor._collect_high_entropy_spans`)
    and span snapping
    (:meth:`creek.redact.redactor.Redactor._snap_to_candidate_runs`).
    Those three used to run their own ``HIGH_ENTROPY_CANDIDATE`` sweeps;
    #909 and #942 each had to be applied to every copy by hand, and #945
    is what happened when a new fact reached only some of them — the
    redactor rewrote its own ``[REDACTED:email_password_combo]`` marker
    into ``[REDACTED:[REDACTED:high_entropy_string]]`` on every re-run,
    while ``--scan`` reported the same phantom finding and the fail-loud
    ``creek process`` redaction stage refused on a count no amount of
    ``--apply`` could clear.

    A run is skipped only when it is **byte-equal** to a run inside one
    of *this instance's* rendered markers **and** the whole marker
    literal sits at exactly that run's offset in *text*. The carve-out is
    therefore **name-keyed, not shape-keyed**: nothing about the
    ``[REDACTED:...]`` form is privileged, so a forged
    ``[REDACTED:<real secret>]`` is still a candidate and still redacted.
    Two deliberate non-goals follow from that and are not oversights:

    - the 21 regex detectors keep firing inside marker text, because
      suppressing them is what would let a forged marker hide a live key;
    - an operator ``custom_patterns`` regex that matches its own marker
      still self-matches, for the same reason — a regex-side carve-out
      would narrow the guard rather than this one.

    The bound on how much this can ever exempt: ``HIGH_ENTROPY_CANDIDATE``
    matches maximally, so a secret written flush against a marker forms a
    *longer* run than the marker's own, fails byte-equality, and stays a
    candidate. The exempt bytes are always a pattern *name* inside a
    literal marker at an exact offset, never operator data.

    Cost is one dict lookup per candidate run — nothing when the map is
    empty, which is the case for any template whose markers hold no run
    of :data:`~creek.redact.patterns.HIGH_ENTROPY_MIN_RUN` characters. No
    extra pass is made over *text*.

    Args:
        text: The line or document to sweep for candidate runs.
        marker_runs: This instance's map from :func:`emitted_marker_runs`.
            Pass an empty mapping to sweep with no carve-out at all.

    Yields:
        Each :class:`re.Match` for a run that is not part of a marker
        this configuration would render, in document order.
    """
    for candidate in HIGH_ENTROPY_CANDIDATE.finditer(text):
        occurrences = marker_runs.get(candidate.group())
        if occurrences is not None and any(
            candidate.start() >= offset
            and text.startswith(marker, candidate.start() - offset)
            for marker, offset in occurrences
        ):
            continue
        yield candidate


def emitted_marker_runs(
    replacement_template: str,
    pattern_names: frozenset[str],
) -> MarkerRuns:
    """Precompute the candidate runs inside the markers a config emits.

    Called once per instance, from ``__init__``, so the map is never
    rebuilt per line, per candidate or per ``redact_content`` call.

    *pattern_names* must be the **range of**
    ``Redactor._select_marker_name`` — the instance's full pattern set
    *plus* :data:`HIGH_ENTROPY_PATTERN_NAME`. The entropy detector's own
    name lives in :data:`~creek.redact.patterns.PATTERN_METADATA` but is
    excluded from :data:`~creek.redact.patterns.REDACTION_PATTERNS` by
    ``_DETECTOR_ONLY_PATTERNS``, so it is absent from that pattern set
    while still being the only marker name the detector ever emits.
    Building the map from the pattern set alone would leave that one
    marker un-exempt — latent under the 19-character default rendering,
    live under a legal template such as ``[REDACTED_{name}_END]``.

    It must equally *not* be built from the per-call ``pattern_types``
    filter: a marker written by an earlier whole-vault run has to stay
    inert under a later narrowed call.

    ``replacement_template.format`` is safe for any config that loads —
    :meth:`creek.config.RedactionConfig.validate_replacement_template`
    rejects every placeholder but ``{name}``.

    Args:
        replacement_template: The instance's
            :pyattr:`RedactionConfig.replacement_template`.
        pattern_names: Every name this instance can render a marker for.

    Returns:
        A :data:`MarkerRuns` map, empty when no rendered marker contains
        a run long enough to be a candidate at all.
    """
    found: dict[str, list[tuple[str, int]]] = {}
    for name in sorted(pattern_names):
        marker = replacement_template.format(name=name)
        for run in iter_unmarked_candidates(marker, marker_runs={}):
            occurrences = found.setdefault(run.group(), [])
            occurrence = (marker, run.start())
            if occurrence not in occurrences:
                occurrences.append(occurrence)
    return {text: tuple(occurrences) for text, occurrences in found.items()}


def entropy_threshold(min_confidence: float) -> float:
    """Map a ``min_confidence`` in ``[0, 1]`` to a bits/char entropy threshold.

    The threshold rises linearly between :data:`_ENTROPY_FLOOR_BITS` and
    :data:`_ENTROPY_CEILING_BITS` so callers can express sensitivity as a
    confidence rather than a bit count.

    Args:
        min_confidence: Confidence in ``[0, 1]``; higher = stricter.

    Returns:
        Entropy threshold in bits/char.
    """
    span = _ENTROPY_CEILING_BITS - _ENTROPY_FLOOR_BITS
    return _ENTROPY_FLOOR_BITS + min_confidence * span


def _luhn_valid(digits: str) -> bool:
    """Return ``True`` when *digits* satisfies the Luhn checksum.

    Caller is responsible for stripping non-digit separators (spaces,
    dashes) before invoking — this helper rejects any input containing
    non-digit characters so a typo in the caller is surfaced as a
    rejection rather than a silent miscount.

    Args:
        digits: A non-empty string of decimal digits.

    Returns:
        ``True`` if the digit sequence is Luhn-valid; ``False`` for
        empty input, non-digit input, or a failing checksum.
    """
    if not digits or not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = ord(char) - ord("0")
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


_DIGIT_STRIP = re.compile(r"\D")


def post_validate(pattern_name: str, matched_text: str) -> bool:
    """Run pattern-specific post-validation on a regex hit.

    Public dispatch shared by :class:`RedactionScanner` and
    :class:`creek.redact.redactor.Redactor`. Returns ``True`` when the
    match should be kept and ``False`` when it should be dropped;
    patterns without a registered validator are always kept.

    Args:
        pattern_name: The pattern key that produced the match.
        matched_text: The substring that the regex captured.

    Returns:
        Whether the match passes the pattern's post-validation.
    """
    if pattern_name == "credit_card":
        digits = _DIGIT_STRIP.sub("", matched_text)
        return _luhn_valid(digits)
    return True


def _get_severity(match_type: str) -> str:
    """Look up the severity for a pattern by name.

    Args:
        match_type: Name of the redaction pattern.

    Returns:
        Severity string, or ``"unknown"`` if not in metadata.
    """
    info = PATTERN_METADATA.get(match_type)
    return info.severity if info else "unknown"


_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "unknown": 4,
}


def _severity_rank(severity: str) -> int:
    """Return a numeric rank for sorting by severity (lower = more severe).

    Args:
        severity: Severity string.

    Returns:
        Integer rank for sorting.
    """
    return _SEVERITY_ORDER.get(severity, 4)


def _count_by_severity(matches: list[RedactionMatch]) -> dict[str, int]:
    """Count matches grouped by severity level.

    Args:
        matches: List of redaction matches.

    Returns:
        Dictionary mapping severity to count.
    """
    counts: dict[str, int] = {}
    for match in matches:
        sev = _get_severity(match.match_type)
        counts[sev] = counts.get(sev, 0) + 1
    return counts
