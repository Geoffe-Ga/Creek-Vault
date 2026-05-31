"""Tests for the calibrate-against-own-writing harness (FEAT-040.1)."""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style.calibration import calibrate
from creek.generate.ai_style.model import VoiceFingerprint

_FINGERPRINT = VoiceFingerprint(features={}, fragment_count=50)
_CLEAN_USER_TEXTS = [
    "I kept it simple today and that was enough.",
    "Walked the dog, wrote a little, felt all right about it.",
    "Not my best work but it is honest and it is mine.",
]


class TestCalibrate:
    """Precision is measured against the user's own writing."""

    def test_clean_user_writing_does_not_flag(self) -> None:
        """The headline property: the user's real writing must not flag."""
        report = calibrate(
            _CLEAN_USER_TEXTS,
            fingerprint=_FINGERPRINT,
            config=AIStyleConfig(),
        )
        assert report.false_positive_rate == 0.0
        assert report.passed is True
        assert report.user_texts == 3

    def test_detects_shipped_ai_examples(self) -> None:
        """The shipped AI examples (placeholder dates) are detected."""
        report = calibrate(
            _CLEAN_USER_TEXTS,
            fingerprint=_FINGERPRINT,
            config=AIStyleConfig(),
        )
        assert report.detection_rate == 1.0
        assert report.ai_texts >= 1

    def test_user_text_with_tell_fails_calibration(self) -> None:
        """A false positive above the floor fails the run, loudly."""
        polluted = [*_CLEAN_USER_TEXTS, "draft saved 2025-xx-xx"]
        report = calibrate(
            polluted,
            fingerprint=_FINGERPRINT,
            config=AIStyleConfig(),
        )
        assert report.false_positive_rate > 0.0
        assert report.passed is False

    def test_empty_user_set_passes_vacuously(self) -> None:
        """No user texts ⇒ zero false-positive rate, passes."""
        report = calibrate([], fingerprint=_FINGERPRINT, config=AIStyleConfig())
        assert report.false_positive_rate == 0.0
        assert report.passed is True

    def test_ai_texts_override_empty_zero_detection(self) -> None:
        """An empty positive set yields a zero detection rate."""
        report = calibrate(
            _CLEAN_USER_TEXTS,
            fingerprint=_FINGERPRINT,
            config=AIStyleConfig(),
            ai_texts=[],
        )
        assert report.detection_rate == 0.0

    def test_summary_reports_verdict(self) -> None:
        """The summary line names PASS/FAIL and both rates."""
        report = calibrate(
            _CLEAN_USER_TEXTS,
            fingerprint=_FINGERPRINT,
            config=AIStyleConfig(),
        )
        summary = report.summary()
        assert "PASS" in summary
        assert "calibration" in summary
