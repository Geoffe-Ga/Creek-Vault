"""Calibrate the AI-style detector against the user's own writing.

The decisive correctness property of FEAT-040 is that the detector must
not flag the user's authentic voice. So calibration is asymmetric: the
**negative set is the user's own fragments** (false positives here are the
metric that matters) and the **positive set is shipped AI examples**
(:mod:`creek.generate.ai_style.calibration.examples`). This mirrors the
shape of :mod:`creek.classify.calibration` and its ``DEFAULT_FLOORS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.generate.ai_style.calibration.examples import AI_EXAMPLES
from creek.generate.ai_style.scanner import scan

if TYPE_CHECKING:
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint


@dataclass(frozen=True)
class CalibrationReport:
    """The outcome of a calibration run.

    Attributes:
        false_positive_rate: Fraction of the user's own texts that flagged.
            The headline metric — should be near zero.
        detection_rate: Fraction of shipped AI examples that flagged.
        user_texts: Count of negative-set (user) texts scanned.
        ai_texts: Count of positive-set (AI) texts scanned.
        fp_floor: The configured maximum acceptable false-positive rate.
        passed: ``True`` when ``false_positive_rate <= fp_floor``.
    """

    false_positive_rate: float
    detection_rate: float
    user_texts: int
    ai_texts: int
    fp_floor: float
    passed: bool

    def summary(self) -> str:
        """Return a one-line human-readable summary of the run.

        Returns:
            A compact ``PASS``/``FAIL`` line with both rates.
        """
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"ai-style calibration {verdict}: "
            f"false positives {self.false_positive_rate:.0%} "
            f"(floor {self.fp_floor:.0%}) on {self.user_texts} of your "
            f"fragments; detected {self.detection_rate:.0%} of "
            f"{self.ai_texts} AI examples"
        )


def _flags(text: str, *, fingerprint: VoiceFingerprint, config: AIStyleConfig) -> bool:
    """Return whether *text* trips the detector.

    A text "flags" when it produces any finding or its voice distance
    exceeds the configured upper bound.

    Args:
        text: The text to scan.
        fingerprint: The user's voice fingerprint.
        config: The AI-style configuration.

    Returns:
        ``True`` when the text would be surfaced to the operator.
    """
    report = scan(text, fingerprint=fingerprint, config=config)
    return bool(report.findings) or report.voice_distance > config.voice_distance_upper


def calibrate(
    user_texts: list[str],
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
    ai_texts: list[str] | None = None,
) -> CalibrationReport:
    """Measure detector precision against the user's own writing.

    Args:
        user_texts: The negative set — genuinely user-authored passages.
            Ideally the same fragments the *fingerprint* was built from.
        fingerprint: The user's voice fingerprint.
        config: The AI-style configuration (supplies ``calibration_fp_floor``
            and ``voice_distance_upper``).
        ai_texts: The positive set. Defaults to the shipped
            :data:`AI_EXAMPLES`.

    Returns:
        A :class:`CalibrationReport`. ``passed`` is ``False`` when the user's
        own writing flags above ``config.calibration_fp_floor``.
    """
    positives = AI_EXAMPLES if ai_texts is None else ai_texts

    user_flags = sum(
        1 for t in user_texts if _flags(t, fingerprint=fingerprint, config=config)
    )
    ai_flags = sum(
        1 for t in positives if _flags(t, fingerprint=fingerprint, config=config)
    )

    fp_rate = user_flags / len(user_texts) if user_texts else 0.0
    detection_rate = ai_flags / len(positives) if positives else 0.0

    return CalibrationReport(
        false_positive_rate=fp_rate,
        detection_rate=detection_rate,
        user_texts=len(user_texts),
        ai_texts=len(positives),
        fp_floor=config.calibration_fp_floor,
        passed=fp_rate <= config.calibration_fp_floor,
    )
