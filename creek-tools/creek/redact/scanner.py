"""Redaction scanner — detect sensitive data in files without storing it.

The :class:`RedactionScanner` walks files line-by-line, matches against
both built-in and custom regex patterns, and returns :class:`RedactionMatch`
objects that contain only a salted SHA-256 hash of the matched text — never
the text itself.

A per-session random salt (16 bytes from :func:`os.urandom`) ensures that
hashes cannot be reversed via rainbow tables while still allowing
deduplication within a single scan session.
"""

import hashlib
import os
import re
from pathlib import Path

from pydantic import BaseModel

from creek.config import RedactionConfig
from creek.redact.patterns import REDACTION_PATTERNS


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
        return hashlib.sha256(  # nosec B324
            self.salt + text.encode()
        ).hexdigest()

    def _is_allowlisted(self, text: str) -> bool:
        """Check whether *text* appears in the false-positive allowlist.

        Args:
            text: The matched string to check.

        Returns:
            ``True`` if the string should be excluded from results.
        """
        return text in self.config.false_positive_allowlist

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

    def scan_directory(self, dir_path: Path) -> list[RedactionMatch]:
        """Recursively scan all files in a directory for sensitive data.

        Skips binary files by catching encoding errors gracefully.

        Args:
            dir_path: Path to the directory to scan.

        Returns:
            Aggregated list of :class:`RedactionMatch` objects.

        Raises:
            FileNotFoundError: If *dir_path* does not exist.
        """
        if not dir_path.exists():
            msg = f"Directory not found: {dir_path}"
            raise FileNotFoundError(msg)

        matches: list[RedactionMatch] = []
        for child in sorted(dir_path.rglob("*")):
            if child.is_file():
                matches.extend(self.scan_file(child))
        return matches

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
