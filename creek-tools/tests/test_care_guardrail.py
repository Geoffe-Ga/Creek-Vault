"""Care guardrail — medication policy + acute-distress detector (#753).

Two halves:

- ``CARE_POLICY`` — the prompt-level policy woven into every voice-generating
  prompt: never advise changing/stopping psychiatric medication; defer to the
  user + their prescriber; point to human support on acute distress; keep the
  ordinary non-directive voice and do not pathologize dark/reflective writing.
- ``acute_distress_guard`` — a conservative, **acute-only** detector returning a
  stable reason code on imminent self-harm/suicidal intent and ``None`` for
  ordinary dark journaling. Biased toward false negatives so reflective writing
  is never flagged.
"""

from __future__ import annotations

from creek.care.guardrail import CARE_POLICY, CARE_SIGNAL, acute_distress_guard


def test_care_policy_covers_medication_and_deferral() -> None:
    """The policy explicitly addresses medication and defers to the prescriber."""
    text = CARE_POLICY.lower()
    assert "medication" in text
    assert "prescriber" in text


def test_care_policy_preserves_non_pathologizing_voice() -> None:
    """The policy tells the model not to pathologize ordinary dark writing."""
    assert "pathologize" in CARE_POLICY.lower()


def test_acute_distress_guard_fires_on_imminent_intent() -> None:
    """Stated, first-person imminent self-harm intent returns the reason code."""
    for entry in (
        "I am going to kill myself tonight.",
        "I've decided to end my life.",
        "i'm going to hurt myself now",
        "I keep thinking about committing suicide and have a plan.",
    ):
        assert acute_distress_guard(entry) == "acute_distress_markers", entry


def test_acute_distress_guard_does_not_pathologize_ordinary_dark_writing() -> None:
    """Affect and reflection — even bleak — must NOT trip the guard."""
    for entry in (
        "I feel hopeless and exhausted lately.",
        "I keep thinking about death and impermanence.",
        "This grief is so heavy I can barely move.",
        "Sometimes I just want it all to stop for a while.",
        "The deadline is killing me.",
        "",
    ):
        assert acute_distress_guard(entry) is None, entry


def test_care_signal_points_to_human_and_professional_support() -> None:
    """The structured care signal names human + professional support, not a bot."""
    assert CARE_SIGNAL["kind"] == "acute_distress"
    assert isinstance(CARE_SIGNAL["message"], str) and CARE_SIGNAL["message"]
    resources = CARE_SIGNAL["resources"]
    assert isinstance(resources, list) and resources
    assert all("name" in r and "contact" in r for r in resources)
