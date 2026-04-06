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
- JSON report generation with match metadata
- Markdown summary grouped by file and severity
- Review queue with context for human review
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — Pydantic needs Path at runtime

from pydantic import BaseModel
from tqdm import tqdm

from creek.config import RedactionConfig  # noqa: TC001 — used at runtime
from creek.redact.patterns import PATTERN_METADATA, REDACTION_PATTERNS

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
    """

    matches: list[RedactionMatch] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped_binary: int = 0
    files_skipped_extension: int = 0


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

    def _build_patterns(self) -> dict[str, re.Pattern[str]]:
        """Merge built-in patterns with any custom patterns from config.

        Returns:
            Combined dictionary of pattern name to compiled regex.
        """
        patterns: dict[str, re.Pattern[str]] = dict(REDACTION_PATTERNS)
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
                    matches.append(
                        RedactionMatch(
                            file_path=file_path,
                            line_number=line_num,
                            match_type=name,
                            salted_hash=self._hash_match(matched_text),
                        )
                    )

        return matches

    def scan_directory(
        self,
        dir_path: Path,
        *,
        progress: bool = False,
    ) -> list[RedactionMatch]:
        """Recursively scan all files in a directory for sensitive data.

        Skips binary files and files with unsupported extensions.
        Respects exclusion patterns from configuration.

        Args:
            dir_path: Path to the directory to scan.
            progress: If ``True``, display a tqdm progress bar.

        Returns:
            Aggregated list of :class:`RedactionMatch` objects.

        Raises:
            FileNotFoundError: If *dir_path* does not exist.
        """
        summary = self.scan_batch(dir_path, progress=progress)
        return summary.matches

    def scan_batch(
        self,
        dir_path: Path,
        *,
        progress: bool = False,
    ) -> ScanSummary:
        """Recursively scan a directory and return a full scan summary.

        Returns a :class:`ScanSummary` with match details and statistics
        about files scanned, skipped (binary), and skipped (extension).

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

        candidates = sorted(child for child in dir_path.rglob("*") if child.is_file())

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
        )

    def generate_report(self, matches: list[RedactionMatch]) -> str:
        """Generate a human-readable report from a list of matches.

        Args:
            matches: Redaction matches to summarise.

        Returns:
            Multi-line string report suitable for console output.
        """
        if not matches:
            return "Redaction scan complete: 0 findings."

        lines: list[str] = [
            f"Redaction scan complete: {len(matches)} finding(s).",
            "",
        ]

        by_type: dict[str, int] = {}
        by_file: dict[str, list[RedactionMatch]] = {}

        for match in matches:
            by_type[match.match_type] = by_type.get(match.match_type, 0) + 1
            file_key = str(match.file_path)
            by_file.setdefault(file_key, []).append(match)

        lines.append("By type:")
        for match_type, count in sorted(by_type.items()):
            lines.append(f"  {match_type}: {count}")

        lines.append("")
        lines.append("By file:")
        for file_key, file_matches in sorted(by_file.items()):
            lines.append(f"  {file_key}:")
            for fm in file_matches:
                lines.append(f"    line {fm.line_number}: {fm.match_type}")

        return "\n".join(lines)

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

    def generate_json_report(
        self,
        summary: ScanSummary,
        output_path: Path,
    ) -> None:
        """Write a structured JSON report to *output_path*.

        The report contains scan statistics and match details grouped
        by file and sorted by severity.

        Args:
            summary: The scan summary to serialise.
            output_path: Destination file path for the JSON report.
        """
        by_file: dict[str, list[dict[str, object]]] = defaultdict(list)

        for match in summary.matches:
            severity = _get_severity(match.match_type)
            by_file[str(match.file_path)].append(
                {
                    "line_number": match.line_number,
                    "match_type": match.match_type,
                    "severity": severity,
                    "salted_hash": match.salted_hash,
                }
            )

        # Sort each file's matches by severity rank.
        for file_matches in by_file.values():
            file_matches.sort(key=lambda m: _severity_rank(str(m["severity"])))

        report: dict[str, object] = {
            "scan_statistics": {
                "files_scanned": summary.files_scanned,
                "files_skipped_binary": summary.files_skipped_binary,
                "files_skipped_extension": summary.files_skipped_extension,
                "total_findings": len(summary.matches),
                "by_severity": _count_by_severity(summary.matches),
                "by_type": _count_by_type(summary.matches),
            },
            "findings_by_file": dict(by_file),
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def generate_markdown_summary(self, summary: ScanSummary) -> str:
        """Generate a human-readable markdown summary of scan results.

        Results are grouped by file and sorted by severity within
        each file section.

        Args:
            summary: The scan summary to format.

        Returns:
            Markdown-formatted string.
        """
        if not summary.matches:
            return "# Redaction Scan Summary\n\nNo findings.\n"

        lines: list[str] = [
            "# Redaction Scan Summary",
            "",
            "## Statistics",
            "",
            f"- **Files scanned**: {summary.files_scanned}",
            f"- **Files skipped (binary)**: {summary.files_skipped_binary}",
            f"- **Files skipped (extension)**: {summary.files_skipped_extension}",
            f"- **Total findings**: {len(summary.matches)}",
            "",
        ]

        # Severity breakdown.
        by_sev = _count_by_severity(summary.matches)
        if by_sev:
            lines.append("### By Severity")
            lines.append("")
            for sev in ("critical", "high", "medium", "low"):
                if sev in by_sev:
                    lines.append(f"- **{sev}**: {by_sev[sev]}")
            lines.append("")

        # Group by file.
        by_file: dict[str, list[RedactionMatch]] = defaultdict(list)
        for match in summary.matches:
            by_file[str(match.file_path)].append(match)

        lines.append("## Findings by File")
        lines.append("")

        for file_key in sorted(by_file):
            file_matches = sorted(
                by_file[file_key],
                key=lambda m: _severity_rank(_get_severity(m.match_type)),
            )
            lines.append(f"### `{file_key}`")
            lines.append("")
            lines.append("| Line | Type | Severity |")
            lines.append("|------|------|----------|")
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
            lines.append(f"## `{file_key}`")
            lines.append("")

            for fm in file_matches:
                finding_num += 1
                severity = _get_severity(fm.match_type)
                context = self.extract_context(fm.file_path, fm.line_number)

                lines.append(f"### Finding {finding_num}")
                lines.append("")
                lines.append(f"- **Type**: {fm.match_type}")
                lines.append(f"- **Severity**: {severity}")
                lines.append(f"- **Line**: {fm.line_number}")
                lines.append("")

                if context:
                    lines.append("```")
                    lines.extend(context)
                    lines.append("```")
                    lines.append("")

                lines.append(f"- [ ] Confirmed sensitive (finding {finding_num})")
                lines.append(f"- [ ] False positive (finding {finding_num})")
                lines.append("")

        return "\n".join(lines)


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


def _count_by_type(matches: list[RedactionMatch]) -> dict[str, int]:
    """Count matches grouped by pattern type.

    Args:
        matches: List of redaction matches.

    Returns:
        Dictionary mapping match type to count.
    """
    counts: dict[str, int] = {}
    for match in matches:
        counts[match.match_type] = counts.get(match.match_type, 0) + 1
    return counts
