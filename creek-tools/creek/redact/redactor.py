"""Redactor — replace sensitive data with safe markers and log redactions.

The :class:`Redactor` re-scans content to locate match positions (since
:class:`RedactionMatch` intentionally does **not** store the matched text)
and replaces each hit with a ``[REDACTED:type]`` marker.

Replacement is a single pass over the *original* content: every in-scope
pattern is matched against the untouched text, truly overlapping spans
are unioned, and the merged spans are spliced out right-to-left. Because
no pattern ever anchors on partially redacted text, a ``--scan`` finding
cannot survive a ``--apply`` step (Issue #832).

Redaction logs are written as JSON with a session salt in the header so
that hashes can be correlated within a session but not reversed.
"""

import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from creek.config import RedactionConfig
from creek.redact.patterns import PATTERN_METADATA, REDACTION_PATTERNS
from creek.redact.scanner import (
    HIGH_ENTROPY_CANDIDATE,
    HIGH_ENTROPY_PATTERN_NAME,
    RedactionMatch,
    entropy_threshold,
    post_validate,
    shannon_entropy,
)

_SEVERITY_RANKS: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
"""Marker-selection rank per metadata severity (lower = more severe)."""

_UNKNOWN_SEVERITY_RANK: int = len(_SEVERITY_RANKS)
"""Rank for patterns without metadata — sorts below every known severity."""


class _Span(NamedTuple):
    """A single pattern match located against the original content.

    Attributes:
        start: Offset of the first matched character.
        end: Offset one past the last matched character.
        pattern_name: The pattern key that produced the match.
        order: Collection order, used as the final marker tie-break.
    """

    start: int
    end: int
    pattern_name: str
    order: int


class _MergedSpan(NamedTuple):
    """A maximal union of truly overlapping match spans.

    Attributes:
        start: Offset where the merged region begins.
        end: Offset one past the merged region's last character.
        contributors: The individual matches folded into this region.
    """

    start: int
    end: int
    contributors: list[_Span]


def _severity_rank(pattern_name: str) -> int:
    """Rank a pattern for marker selection (lower = more severe).

    Args:
        pattern_name: The pattern key to rank.

    Returns:
        The rank of the pattern's metadata severity, or
        :data:`_UNKNOWN_SEVERITY_RANK` when the pattern (e.g. a custom
        one) has no metadata entry.
    """
    info = PATTERN_METADATA.get(pattern_name)
    if info is None:
        return _UNKNOWN_SEVERITY_RANK
    return _SEVERITY_RANKS.get(info.severity, _UNKNOWN_SEVERITY_RANK)


def _merge_spans(spans: list[_Span]) -> list[_MergedSpan]:
    """Union truly overlapping spans into maximal merged regions.

    Spans that merely touch (one starting exactly where the previous
    ends) stay separate so adjacent distinct findings keep their own
    markers; only genuine overlap (``start < previous end``) merges.

    Args:
        spans: Match spans collected against the original content.

    Returns:
        Non-overlapping merged spans ordered by start offset.
    """
    merged: list[_MergedSpan] = []
    for span in sorted(spans, key=lambda s: s.start):
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = _MergedSpan(
                start=last.start,
                end=max(last.end, span.end),
                contributors=[*last.contributors, span],
            )
        else:
            merged.append(_MergedSpan(span.start, span.end, [span]))
    return merged


def _select_marker_name(contributors: list[_Span]) -> str:
    """Choose which pattern name labels a merged span's marker.

    The most severe contributing pattern wins; ties fall to the widest
    individual span, then to whichever match was collected first.

    Args:
        contributors: The matches folded into one merged span.

    Returns:
        The winning pattern name for the replacement marker.
    """

    def _key(span: _Span) -> tuple[int, int, int]:
        """Build the sort key selecting the winning contributor via ``min``.

        Args:
            span: A contributing span to rank.

        Returns:
            Tuple of severity rank, negated width, and collection order.
        """
        return (
            _severity_rank(span.pattern_name),
            span.start - span.end,
            span.order,
        )

    return min(contributors, key=_key).pattern_name


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
        patterns: dict[str, re.Pattern[str]] = REDACTION_PATTERNS.copy()
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
        text is never stored in :class:`RedactionMatch`) in a single pass
        over the original text: every in-scope pattern is matched against
        the untouched content, truly overlapping spans are unioned, and
        each merged region is replaced by the marker of its most severe
        contributor. This guarantees scan/apply parity — a ``--scan``
        finding cannot survive ``--apply``, because no pattern ever
        anchors on already-redacted text (Issue #832).

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

        spans = self._collect_spans(content, patterns_to_use)
        content = self._splice_markers(content, _merge_spans(spans))

        if self._should_apply_high_entropy(pattern_types):
            content = self._replace_high_entropy(content)

        return content

    def _collect_spans(
        self,
        content: str,
        patterns: dict[str, re.Pattern[str]],
    ) -> list[_Span]:
        """Locate every non-allowlisted, validated match in *content*.

        Args:
            content: The original, untouched text.
            patterns: In-scope mapping of pattern name to compiled regex.

        Returns:
            Spans for every match that survives the allowlist and the
            pattern-specific post-validator (e.g. Luhn for
            ``credit_card``), in collection order.
        """
        spans: list[_Span] = []
        for name, pattern in patterns.items():
            for match in pattern.finditer(content):
                text = match.group()
                if self._is_allowlisted(text):
                    continue
                if not post_validate(name, text):
                    continue
                spans.append(_Span(match.start(), match.end(), name, len(spans)))
        return spans

    def _splice_markers(self, content: str, merged: list[_MergedSpan]) -> str:
        """Replace each merged span in *content* with its winning marker.

        Splices right-to-left so earlier offsets stay valid while later
        regions are rewritten.

        Args:
            content: The original text the spans were collected against.
            merged: Non-overlapping merged spans, ordered by start.

        Returns:
            Content with every merged span replaced by a marker.
        """
        for span in reversed(merged):
            marker = self.config.replacement_template.format(
                name=_select_marker_name(span.contributors),
            )
            start, end = span.start, span.end
            content = content[:start] + marker + content[end:]
        return content

    def _should_apply_high_entropy(
        self,
        pattern_types: list[str] | None,
    ) -> bool:
        """Decide whether the entropy detector should run for this call.

        Args:
            pattern_types: Caller-supplied filter; ``None`` means *all*.

        Returns:
            ``True`` when the high-entropy detector is in scope.
        """
        if pattern_types is None:
            return True
        return HIGH_ENTROPY_PATTERN_NAME in pattern_types

    def _replace_high_entropy(self, content: str) -> str:
        """Replace high-entropy substrings with the configured marker.

        Mirrors :meth:`creek.redact.scanner.RedactionScanner._scan_high_entropy`
        so a `--scan` finding cannot survive a `--apply` step. The shared
        ``HIGH_ENTROPY_CANDIDATE`` regex and ``shannon_entropy`` helper
        keep both paths in lockstep.
        """
        marker = self.config.replacement_template.format(
            name=HIGH_ENTROPY_PATTERN_NAME,
        )
        threshold = entropy_threshold(self.config.min_confidence)

        def _replacer(m: re.Match[str]) -> str:
            text = m.group()
            if self._is_allowlisted(text):
                return text
            if shannon_entropy(text) < threshold:
                return text
            return marker

        return HIGH_ENTROPY_CANDIDATE.sub(_replacer, content)

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
