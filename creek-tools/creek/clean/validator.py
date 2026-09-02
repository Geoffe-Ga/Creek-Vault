"""Post-ingestion fragment validator — required fields, encoding, timestamps.

Validates :class:`~creek.models.Fragment` instances against configurable
rules before they enter the vault.  Invalid fragments are routed to a
review queue rather than discarded silently.

Validation checks:
- **Required fields** — source platform, fragment ID, content body (title)
- **UTF-8 encoding** — mojibake and replacement-character detection
- **ISO 8601 timestamps** — rejects future dates and pre-2000 dates
- **Author field** — interlocutor present and non-empty
- **Content length** — configurable minimum character threshold

A timestamp that arrives naive is anchored to America/Los_Angeles through
:func:`creek.time.ensure_aware`, the pipeline's single anchor helper. The
ontology (§8.3) normalises every timestamp Creek writes to LA, so an
offsetless value is an LA wall clock that lost its offset in transit — not
a UTC one. This module carried its own UTC-anchoring copy of that helper
until #1115 consolidated the two; they agreed on aware inputs and
disagreed by the LA offset on every naive one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from creek.time import ensure_aware

if TYPE_CHECKING:
    from creek.models import Fragment

# ---------------------------------------------------------------------------
# Mojibake detection patterns
# ---------------------------------------------------------------------------

_MOJIBAKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # UTF-8 bytes decoded as Latin-1 (Ã followed by a Latin char)
    re.compile(r"\xc3[\x80-\xbf]"),
    # Windows-1252 artefacts (smart quotes, dashes, etc.)
    re.compile("â€[\u2019\u0153\u009c\u009d\u0093\u0094\u0098\u0099]"),
    # Unicode replacement character
    re.compile("\ufffd"),
)
"""Common mojibake byte patterns produced by encoding mismatches."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_YEAR: int = 2000
"""Earliest valid year for fragment timestamps."""

_DEFAULT_MIN_CONTENT_LENGTH: int = 20
"""Default minimum content length in characters."""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class Violation(BaseModel):
    """A single validation violation.

    Attributes:
        field: The fragment field that failed validation.
        code: Machine-readable violation code.
        message: Human-readable explanation.
    """

    field: str
    code: str
    message: str


class ValidationResult(BaseModel):
    """Structured result from fragment validation.

    Attributes:
        is_valid: Whether the fragment passed all checks.
        violations: List of violations found (empty if valid).
        action: Recommended routing — ``accept`` or ``review``.
    """

    is_valid: bool
    violations: list[Violation] = Field(default_factory=list)
    action: Literal["accept", "review"] = "accept"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class FragmentValidator:
    """Validate fragments before vault ingestion.

    Runs five configurable checks against a
    :class:`~creek.models.Fragment` and returns a
    :class:`ValidationResult` with all violations found.  Invalid
    fragments are routed to the review queue (``action="review"``).

    Attributes:
        min_content_length: Minimum character count for content body.
    """

    def __init__(
        self,
        *,
        min_content_length: int = _DEFAULT_MIN_CONTENT_LENGTH,
    ) -> None:
        """Initialise the validator with configurable thresholds.

        Args:
            min_content_length: Minimum character count for the fragment
                title (content body).  Defaults to 20.
        """
        self.min_content_length = min_content_length

    def validate(self, fragment: Fragment) -> ValidationResult:
        """Run all validation checks against a fragment.

        Checks are executed in sequence and all violations are
        accumulated — validation does not short-circuit on the first
        failure.

        Args:
            fragment: The fragment to validate.

        Returns:
            A :class:`ValidationResult` with ``is_valid``, violations,
            and a routing action (``accept`` or ``review``).
        """
        violations: list[Violation] = []

        violations.extend(self._check_required_fields(fragment))
        violations.extend(self._check_encoding(fragment))
        violations.extend(self._check_timestamps(fragment))
        violations.extend(self._check_author(fragment))
        violations.extend(self._check_content_length(fragment))

        is_valid = len(violations) == 0
        action: Literal["accept", "review"] = "accept" if is_valid else "review"

        return ValidationResult(
            is_valid=is_valid,
            violations=violations,
            action=action,
        )

    # ---- Individual checks ----

    def _check_required_fields(
        self,
        fragment: Fragment,
    ) -> list[Violation]:
        """Check that required fields are present and non-empty.

        Args:
            fragment: The fragment to check.

        Returns:
            List of violations for missing required fields.
        """
        violations: list[Violation] = []

        if not fragment.id or not fragment.id.strip():
            violations.append(
                Violation(
                    field="id",
                    code="required",
                    message="Fragment ID is required",
                ),
            )

        if not fragment.title or not fragment.title.strip():
            violations.append(
                Violation(
                    field="title",
                    code="required",
                    message="Content body (title) is required",
                ),
            )

        return violations

    def _check_encoding(
        self,
        fragment: Fragment,
    ) -> list[Violation]:
        """Detect mojibake and encoding corruption in the title.

        Args:
            fragment: The fragment to check.

        Returns:
            List of violations if mojibake patterns are detected.
        """
        violations: list[Violation] = []
        title = fragment.title

        if not title:
            return violations

        for pattern in _MOJIBAKE_PATTERNS:
            if pattern.search(title):
                violations.append(
                    Violation(
                        field="title",
                        code="mojibake",
                        message="Encoding corruption detected in content",
                    ),
                )
                break

        return violations

    def _check_timestamps(
        self,
        fragment: Fragment,
    ) -> list[Violation]:
        """Validate created and ingested timestamps.

        Rejects future dates and dates before 2000-01-01.

        A naive timestamp is anchored through
        :func:`creek.time.ensure_aware`, which attaches
        America/Los_Angeles — the ontology's anchor (§8.3) for every
        timestamp Creek writes, so an offsetless value is an LA wall clock
        that lost its offset in transit. This module used to anchor to UTC
        with a local copy of the helper, which read the same wall clock
        7-8 h earlier than the rest of the pipeline did (#1115).

        The two verdicts move differently under that correction, and both
        directions are safe:

        * ``future_date`` **tightens**. An LA-anchored wall clock is a
          later absolute instant than the same clock read as UTC, so
          strictly more values exceed ``now``. Never fewer.
        * ``pre_2000`` is **unchanged**. ``ensure_aware`` attaches without
          converting, and ``ts_aware.year`` reads a wall-clock field, so
          the year is identical under either anchor.

        Args:
            fragment: The fragment to check.

        Returns:
            List of violations for invalid timestamps.
        """
        violations: list[Violation] = []
        now = datetime.now(tz=UTC)

        for field_name in ("created", "ingested"):
            ts: datetime = getattr(fragment, field_name)
            ts_aware = ensure_aware(ts)

            if ts_aware > now:
                violations.append(
                    Violation(
                        field=field_name,
                        code="future_date",
                        message=(
                            f"Timestamp {field_name} is in the future: {ts.isoformat()}"
                        ),
                    ),
                )

            if ts_aware.year < _MIN_YEAR:
                violations.append(
                    Violation(
                        field=field_name,
                        code="pre_2000",
                        message=(
                            f"Timestamp {field_name} is before "
                            f"year 2000: {ts.isoformat()}"
                        ),
                    ),
                )

        return violations

    def _check_author(
        self,
        fragment: Fragment,
    ) -> list[Violation]:
        """Check that the author (interlocutor) field is present.

        Args:
            fragment: The fragment to check.

        Returns:
            List of violations if author is missing.
        """
        violations: list[Violation] = []
        interlocutor = fragment.source.interlocutor

        if not interlocutor or not interlocutor.strip():
            violations.append(
                Violation(
                    field="source.interlocutor",
                    code="missing_author",
                    message="Author (interlocutor) field is required",
                ),
            )

        return violations

    def _check_content_length(
        self,
        fragment: Fragment,
    ) -> list[Violation]:
        """Check that content meets the minimum length threshold.

        Args:
            fragment: The fragment to check.

        Returns:
            List of violations if content is too short.
        """
        violations: list[Violation] = []
        title = fragment.title.strip()

        if title and len(title) < self.min_content_length:
            violations.append(
                Violation(
                    field="title",
                    code="too_short",
                    message=(
                        f"Content body is too short: {len(title)} "
                        f"chars (minimum {self.min_content_length})"
                    ),
                ),
            )

        return violations
