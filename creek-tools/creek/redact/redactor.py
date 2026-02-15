"""Redactor — replace sensitive data with safe markers and log redactions.

The :class:`Redactor` re-scans content to locate match positions (since
:class:`RedactionMatch` intentionally does **not** store the matched text)
and replaces each hit with a ``[REDACTED:type]`` marker.

Redaction logs are written as JSON with a session salt in the header so
that hashes can be correlated within a session but not reversed.
"""

import json
import re
from pathlib import Path
from typing import Any

from creek.config import RedactionConfig
from creek.redact.patterns import REDACTION_PATTERNS
from creek.redact.scanner import RedactionMatch


class Redactor:
    """Replace sensitive data in text and log redactions.

    Because :class:`RedactionMatch` never stores matched text, the
    redactor must re-scan content using the same patterns to locate
    replacement positions.

    Args:
        config: Redaction configuration (allowlist, custom patterns).
        salt: The session salt used by the scanner that produced the
            matches — stored in the log header for correlation.
    """

    def __init__(self, config: RedactionConfig, salt: bytes) -> None:
        """Initialise the redactor with config and session salt.

        Args:
            config: Redaction configuration.
            salt: Session salt from the corresponding scanner.
        """
        self.config = config
        self.salt = salt
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

    def _is_allowlisted(self, text: str) -> bool:
        """Check whether *text* appears in the false-positive allowlist.

        Args:
            text: The matched string to check.

        Returns:
            ``True`` if the string should be excluded from redaction.
        """
        return text in self.config.false_positive_allowlist

    def redact_content(
        self,
        content: str,
        pattern_types: list[str] | None = None,
    ) -> str:
        """Replace sensitive data in *content* with ``[REDACTED:type]`` markers.

        Re-scans *content* against the configured patterns (since matched
        text is never stored in :class:`RedactionMatch`) and substitutes
        each hit that is not on the allowlist.

        Args:
            content: The text to redact.
            pattern_types: If provided, only apply these pattern names.
                Defaults to all configured patterns.

        Returns:
            A copy of *content* with sensitive data replaced.
        """
        patterns_to_use: dict[str, re.Pattern[str]]
        if pattern_types is not None:
            patterns_to_use = {
                k: v for k, v in self._patterns.items() if k in pattern_types
            }
        else:
            patterns_to_use = self._patterns

        for name, pattern in patterns_to_use.items():
            marker = f"[REDACTED:{name}]"
            content = self._replace_pattern(content, pattern, marker)

        return content

    def _replace_pattern(
        self,
        content: str,
        pattern: re.Pattern[str],
        marker: str,
    ) -> str:
        """Replace all occurrences of *pattern* in *content* with *marker*.

        Skips allowlisted matches so they remain in the output.

        Args:
            content: Source text.
            pattern: Compiled regex to search for.
            marker: Replacement string (e.g. ``[REDACTED:ssn]``).

        Returns:
            Content with non-allowlisted matches replaced.
        """

        def _replacer(m: re.Match[str]) -> str:
            """Return the marker unless the match is allowlisted.

            Args:
                m: Regex match object.

            Returns:
                The marker string or the original match text.
            """
            if self._is_allowlisted(m.group()):
                return m.group()
            return marker

        return pattern.sub(_replacer, content)

    def log_redactions(
        self,
        matches: list[RedactionMatch],
        log_path: Path,
    ) -> None:
        """Write redaction matches to a JSON log file.

        If the log file already exists, appends to the ``entries`` list.
        The log header includes the hex-encoded session salt so hashes
        can be correlated within the same scan session.

        Args:
            matches: List of redaction matches to log.
            log_path: Path to the JSON log file.
        """
        data: dict[str, Any]
        entries: list[Any]

        if log_path.exists():
            data = json.loads(log_path.read_text())
            entries = list(data.get("entries", []))
        else:
            data = {"salt_hex": self.salt.hex()}
            entries = []

        for match in matches:
            entries.append(match.model_dump(mode="json"))

        data["entries"] = entries
        log_path.write_text(json.dumps(data, indent=2))
