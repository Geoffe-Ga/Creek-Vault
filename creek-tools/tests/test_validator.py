"""Tests for creek.clean.validator — post-ingestion fragment validation.

Tests cover:
- ValidationResult dataclass structure
- Required fields validation (source platform, fragment ID, content body)
- UTF-8 encoding and mojibake detection
- ISO 8601 timestamp validation (future dates, pre-2000 dates)
- Author field presence and type classification
- Configurable minimum content length threshold
- Multiple violation accumulation
- Valid fragments passing all checks
"""

from datetime import UTC, datetime, timedelta

from creek.clean.validator import (
    FragmentValidator,
    ValidationResult,
    Violation,
)
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.test_time import OffsetlessTimezone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fragment(**overrides: object) -> Fragment:
    """Build a valid Fragment with sensible defaults, applying overrides."""
    from creek.models import synthetic_fragment_id

    defaults: dict[str, object] = {
        "id": synthetic_fragment_id(),
        "title": ("A sufficiently long content body for validation testing purposes"),
        "source": FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor="alice",
        ),
        "created": datetime(2024, 6, 15, tzinfo=UTC),
        "ingested": datetime(2024, 6, 15, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Fragment(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Tests for the ValidationResult model."""

    def test_valid_result_has_no_violations(self) -> None:
        """A valid result should have is_valid=True and empty violations."""
        result = ValidationResult(is_valid=True, violations=[])
        assert result.is_valid is True
        assert result.violations == []

    def test_invalid_result_has_violations(self) -> None:
        """An invalid result should list its violations."""
        violation = Violation(
            field="title",
            code="required",
            message="Title is required",
        )
        result = ValidationResult(
            is_valid=False,
            violations=[violation],
        )
        assert result.is_valid is False
        assert len(result.violations) == 1
        assert result.violations[0].field == "title"

    def test_violation_fields(self) -> None:
        """Violation should have field, code, and message attributes."""
        v = Violation(
            field="created",
            code="future_date",
            message="Date is in future",
        )
        assert v.field == "created"
        assert v.code == "future_date"
        assert v.message == "Date is in future"


# ---------------------------------------------------------------------------
# FragmentValidator — valid fragments
# ---------------------------------------------------------------------------


class TestValidFragments:
    """Tests for fragments that should pass all validation checks."""

    def test_valid_fragment_passes(self) -> None:
        """A well-formed fragment should pass validation."""
        validator = FragmentValidator()
        fragment = _make_fragment()
        result = validator.validate(fragment)
        assert result.is_valid is True
        assert result.violations == []

    def test_valid_fragment_with_long_content(self) -> None:
        """Fragment with content well above min length should pass."""
        validator = FragmentValidator(min_content_length=10)
        fragment = _make_fragment(title="x" * 100)
        result = validator.validate(fragment)
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Required fields validation
# ---------------------------------------------------------------------------


class TestRequiredFields:
    """Tests for required field validation."""

    def test_empty_title_fails(self) -> None:
        """Fragment with empty title should fail required-field check."""
        validator = FragmentValidator()
        fragment = _make_fragment(title="")
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(
            v.code == "required" and v.field == "title" for v in result.violations
        )

    def test_whitespace_only_title_fails(self) -> None:
        """Fragment with whitespace-only title should fail."""
        validator = FragmentValidator()
        fragment = _make_fragment(title="   \t\n  ")
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(
            v.code == "required" and v.field == "title" for v in result.violations
        )

    def test_empty_id_fails(self) -> None:
        """Fragment with empty ID should fail required-field check."""
        validator = FragmentValidator()
        fragment = _make_fragment(id="")
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "required" and v.field == "id" for v in result.violations)


# ---------------------------------------------------------------------------
# Encoding validation
# ---------------------------------------------------------------------------


class TestEncodingValidation:
    """Tests for UTF-8 encoding and mojibake detection."""

    def test_valid_utf8_passes(self) -> None:
        """Clean UTF-8 content should pass encoding check."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            title="Hello, world! Unicode: caf\u00e9 r\u00e9sum\u00e9 na\u00efve",
        )
        result = validator.validate(fragment)
        assert not any(v.code == "mojibake" for v in result.violations)

    def test_mojibake_detected(self) -> None:
        """Common mojibake patterns should be flagged."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            title="This is caf\u00c3\u00a9 and r\u00c3\u00a9sum\u00c3\u00a9 content",
        )
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "mojibake" for v in result.violations)

    def test_replacement_char_detected(self) -> None:
        """Unicode replacement character U+FFFD should be flagged."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            title="Data with \ufffd replacement chars",
        )
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "mojibake" for v in result.violations)

    def test_legitimate_unicode_passes(self) -> None:
        """Legitimate unicode should not be flagged as mojibake."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            title="\u65e5\u672c\u8a9e \u97d3\u56fd\u8a9e \u4e2d\u6587 text for length",
        )
        result = validator.validate(fragment)
        assert not any(v.code == "mojibake" for v in result.violations)


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    """Tests for ISO 8601 timestamp validation."""

    def test_valid_timestamp_passes(self) -> None:
        """A recent, valid timestamp should pass."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            created=datetime(2024, 3, 15, tzinfo=UTC),
        )
        result = validator.validate(fragment)
        assert not any(v.code == "future_date" for v in result.violations)
        assert not any(v.code == "pre_2000" for v in result.violations)

    def test_future_date_rejected(self) -> None:
        """Timestamps in the future should be rejected."""
        validator = FragmentValidator()
        future = datetime.now(tz=UTC) + timedelta(days=30)
        fragment = _make_fragment(created=future)
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "future_date" for v in result.violations)

    def test_pre_2000_date_rejected(self) -> None:
        """Timestamps before 2000-01-01 should be rejected."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            created=datetime(1999, 12, 31, tzinfo=UTC),
        )
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "pre_2000" for v in result.violations)

    def test_exactly_2000_passes(self) -> None:
        """Timestamp on 2000-01-01 should pass."""
        validator = FragmentValidator()
        fragment = _make_fragment(
            created=datetime(2000, 1, 1, tzinfo=UTC),
        )
        result = validator.validate(fragment)
        assert not any(v.code == "pre_2000" for v in result.violations)

    def test_ingested_timestamp_also_validated(self) -> None:
        """The ingested timestamp should also be validated."""
        validator = FragmentValidator()
        future = datetime.now(tz=UTC) + timedelta(days=30)
        fragment = _make_fragment(ingested=future)
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(
            v.code == "future_date" and v.field == "ingested" for v in result.violations
        )


# ---------------------------------------------------------------------------
# Author validation
# ---------------------------------------------------------------------------


class TestAuthorValidation:
    """Tests for author field presence and type classification."""

    def test_author_present_passes(self) -> None:
        """Fragment with interlocutor set should pass author check."""
        validator = FragmentValidator()
        fragment = _make_fragment()
        result = validator.validate(fragment)
        assert not any(v.code == "missing_author" for v in result.violations)

    def test_missing_author_flagged(self) -> None:
        """Fragment without interlocutor should be flagged."""
        validator = FragmentValidator()
        source = FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor=None,
        )
        fragment = _make_fragment(source=source)
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "missing_author" for v in result.violations)

    def test_empty_author_flagged(self) -> None:
        """Fragment with empty interlocutor should be flagged."""
        validator = FragmentValidator()
        source = FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor="",
        )
        fragment = _make_fragment(source=source)
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "missing_author" for v in result.violations)

    def test_whitespace_author_flagged(self) -> None:
        """Fragment with whitespace-only interlocutor is flagged."""
        validator = FragmentValidator()
        source = FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor="   ",
        )
        fragment = _make_fragment(source=source)
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "missing_author" for v in result.violations)


# ---------------------------------------------------------------------------
# Minimum content length
# ---------------------------------------------------------------------------


class TestMinContentLength:
    """Tests for configurable minimum content length threshold."""

    def test_below_min_length_fails(self) -> None:
        """Content shorter than minimum should fail."""
        validator = FragmentValidator(min_content_length=50)
        fragment = _make_fragment(title="Short")
        result = validator.validate(fragment)
        assert result.is_valid is False
        assert any(v.code == "too_short" for v in result.violations)

    def test_at_min_length_passes(self) -> None:
        """Content exactly at minimum length should pass."""
        validator = FragmentValidator(min_content_length=10)
        fragment = _make_fragment(title="a" * 10)
        result = validator.validate(fragment)
        assert not any(v.code == "too_short" for v in result.violations)

    def test_above_min_length_passes(self) -> None:
        """Content above minimum length should pass."""
        validator = FragmentValidator(min_content_length=5)
        fragment = _make_fragment(title="Hello, World!")
        result = validator.validate(fragment)
        assert not any(v.code == "too_short" for v in result.violations)

    def test_custom_threshold(self) -> None:
        """Minimum content length should be configurable."""
        validator = FragmentValidator(min_content_length=100)
        fragment = _make_fragment(title="x" * 99)
        result = validator.validate(fragment)
        assert any(v.code == "too_short" for v in result.violations)

    def test_default_threshold(self) -> None:
        """Default minimum content length should be 20."""
        validator = FragmentValidator()
        assert validator.min_content_length == 20


# ---------------------------------------------------------------------------
# Multiple violations
# ---------------------------------------------------------------------------


class TestMultipleViolations:
    """Tests for accumulating multiple violations."""

    def test_multiple_violations_accumulated(self) -> None:
        """Validator should report all violations, not stop at first."""
        validator = FragmentValidator(min_content_length=100)
        source = FragmentSource(
            platform=SourcePlatform.CLAUDE,
            interlocutor=None,
        )
        future = datetime.now(tz=UTC) + timedelta(days=30)
        fragment = _make_fragment(
            title="Short",
            source=source,
            created=future,
        )
        result = validator.validate(fragment)
        assert result.is_valid is False
        codes = {v.code for v in result.violations}
        assert "too_short" in codes
        assert "missing_author" in codes
        assert "future_date" in codes

    def test_violation_count_matches_issues(self) -> None:
        """Each distinct issue should produce exactly one violation."""
        validator = FragmentValidator()
        fragment = _make_fragment(title="", id="")
        result = validator.validate(fragment)
        required_violations = [v for v in result.violations if v.code == "required"]
        assert len(required_violations) == 2


# ---------------------------------------------------------------------------
# Review queue routing
# ---------------------------------------------------------------------------


class TestReviewRouting:
    """Tests for routing invalid fragments to review queue."""

    def test_valid_fragment_action_accept(self) -> None:
        """Valid fragments should have action 'accept'."""
        validator = FragmentValidator()
        fragment = _make_fragment()
        result = validator.validate(fragment)
        assert result.action == "accept"

    def test_invalid_fragment_action_review(self) -> None:
        """Invalid fragments should have action 'review'."""
        validator = FragmentValidator()
        fragment = _make_fragment(title="")
        result = validator.validate(fragment)
        assert result.action == "review"


# ---------------------------------------------------------------------------
# Naive timestamp handling
# ---------------------------------------------------------------------------


class TestNaiveTimestamp:
    """Tests for naive datetime handling in timestamp validation."""

    def test_naive_timestamp_is_anchored_to_la_not_utc(self) -> None:
        """A bypassed naive timestamp ages as an LA wall clock (#1115, #1116).

        This replaces a test that could not fail. It built its fragment
        through ``Fragment(...)``, whose ``_normalise_timestamp`` validator
        anchors ``created`` / ``ingested`` to LA before the validator's own
        anchor is ever consulted — so the naive branch was unreachable — and
        it asserted about ``2024-06-15``, a date comfortably past under
        either anchor. Reaching the branch requires one of the documented
        bypasses (``models.py``'s ``_normalise_timestamp`` names all three);
        post-construction attribute assignment is the shape
        ``clean/context.py`` uses in production.

        The probe sits 3 h the past side of ``now`` measured in UTC, which
        is inside the LA offset band in both DST regimes: anchored to UTC it
        is in the past and clean, anchored to LA it is 4-5 h in the future
        and must be flagged. That direction is the tightening this
        consolidation is required to demonstrate — ``future_date`` fires on
        strictly more values, never fewer.
        """
        validator = FragmentValidator()
        fragment = _make_fragment().model_copy(deep=True)
        naive_recent = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=3)
        fragment.created = naive_recent
        fragment.ingested = naive_recent

        # Assert the bypass first: if pydantic ever gains
        # ``validate_assignment`` this test must fail loudly rather than
        # quietly go vacuous the way its predecessor did.
        assert fragment.created.tzinfo is None
        assert fragment.ingested.tzinfo is None

        result = validator.validate(fragment)

        assert any(
            v.code == "future_date" and v.field == "created" for v in result.violations
        )

    def test_pre_2000_verdict_is_offset_independent(self) -> None:
        """Moving the anchor cannot move the ``pre_2000`` verdict (#1115).

        The year is read off the *attached* value, and attaching a zone
        never moves a wall-clock field, so ``2000-01-01T00:30`` is year 2000
        under UTC and under LA alike. The property is already pinned one
        level down at
        ``test_time.py::TestEnsureAware::test_naive_input_keeps_its_wall_clock_fields``;
        this is the thin validator-level extension that keeps the guard
        honest at the surface an operator actually sees. It would fail
        against the plausible *wrong* implementation — ``astimezone``,
        which converts from host-local and on a UTC host renders
        ``1999-12-31T16:30``.
        """
        validator = FragmentValidator()
        fragment = _make_fragment().model_copy(deep=True)
        fragment.created = datetime(2000, 1, 1, 0, 30)

        assert fragment.created.tzinfo is None

        result = validator.validate(fragment)

        assert not any(v.code == "pre_2000" for v in result.violations)

    def test_tzinfo_without_an_offset_is_anchored_rather_than_raising(self) -> None:
        """A tzinfo whose ``utcoffset()`` is ``None`` is repaired (#1115).

        The validator's own anchor tested ``dt.tzinfo is None``, which is
        strictly narrower than the condition CPython uses when comparing:
        a value carrying a ``tzinfo`` that yields no offset is *also*
        naive, passed straight through unrepaired, and raised
        ``TypeError: can't compare offset-naive and offset-aware
        datetimes`` out of the comparison against ``now``. Consolidating
        onto ``creek.time.ensure_aware``, which tests the offset itself,
        deliberately widens the repair to cover it. This is a repair valve
        widening, not an admission gate: strictly more values are made
        well-formed, and no verdict is suppressed.
        """
        validator = FragmentValidator()
        fragment = _make_fragment().model_copy(deep=True)
        fragment.created = datetime(2024, 6, 15, tzinfo=OffsetlessTimezone())

        assert fragment.created.utcoffset() is None

        result = validator.validate(fragment)

        assert not any(v.code == "future_date" for v in result.violations)
        assert not any(v.code == "pre_2000" for v in result.violations)
