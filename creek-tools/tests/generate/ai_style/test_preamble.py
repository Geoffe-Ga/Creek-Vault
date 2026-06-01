"""Tests for the idiolect-driven prompt preamble (FEAT-040.8).

The preamble is the *prevention* lever: it states the user's measured voice
as positive targets and the registry avoids *minus the features the user
genuinely uses*, so generation steers toward this writer rather than away
from a generic AI. These tests pin the data-driven behaviour: targets come
from the fingerprint, a genuinely-used feature is never put on the avoid
list, the thin-fingerprint fallback degrades gracefully, and the output is
length-capped.
"""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.preamble import build_style_preamble
from creek.generate.ai_style.tells import TELL_REGISTRY

# AI-vocabulary tell: an "avoid" tell with a positive margin, so a user whose
# own rate is well above the generic prior counts as genuinely using it.
_AI_VOCAB = TELL_REGISTRY["ai_vocabulary"]


def _fingerprint(rates: dict[str, float], *, fragments: int = 20) -> VoiceFingerprint:
    """Build a fingerprint with *rates* backed by *fragments* each."""
    return VoiceFingerprint(
        features={k: FeatureStat(rate=v, support=fragments) for k, v in rates.items()},
        fragment_count=fragments,
    )


def test_disabled_returns_empty() -> None:
    """``prevent_in_prompt=False`` yields no preamble at all."""
    config = AIStyleConfig(prevent_in_prompt=False)
    assert build_style_preamble(_fingerprint({"em_dash_density": 5.0}), config) == ""


def test_measured_targets_rendered_from_fingerprint() -> None:
    """A measured signature feature appears as a positive target with its rate."""
    config = AIStyleConfig()
    preamble = build_style_preamble(_fingerprint({"em_dash_density": 4.2}), config)
    assert "## Voice targets" in preamble
    assert "em-dash" in preamble
    # The user's own measured number is rendered, not a generic placeholder.
    assert "4.2" in preamble


def test_thin_support_feature_not_rendered_as_target() -> None:
    """A feature with too few backing fragments is not rendered as a target."""
    config = AIStyleConfig()
    # fragment_count high enough to be non-thin overall, but this one feature
    # is backed by too little support to trust as a target.
    fingerprint = VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=9.9, support=2)},
        fragment_count=20,
    )
    preamble = build_style_preamble(fingerprint, config)
    assert "9.9" not in preamble


def test_genuine_feature_excluded_from_avoids() -> None:
    """A word the user genuinely uses is not on the avoid list."""
    config = AIStyleConfig()
    # Rate far above the tell's generic prior + margin => genuinely theirs.
    fingerprint = _fingerprint({_AI_VOCAB.feature_key: 12.0})
    preamble = build_style_preamble(fingerprint, config)
    assert _AI_VOCAB.description not in _avoid_block(preamble)


def test_genuine_feature_kept_as_voice() -> None:
    """A genuinely-used AI-ish feature is surfaced as the writer's own voice."""
    config = AIStyleConfig()
    fingerprint = _fingerprint({_AI_VOCAB.feature_key: 12.0})
    preamble = build_style_preamble(fingerprint, config)
    # It still appears — as something to keep, not avoid.
    assert _AI_VOCAB.description in preamble
    assert _AI_VOCAB.description not in _avoid_block(preamble)


def test_unused_feature_stays_on_avoid_list() -> None:
    """A feature the user does not use remains an avoid."""
    config = AIStyleConfig()
    fingerprint = _fingerprint({_AI_VOCAB.feature_key: 0.0})
    preamble = build_style_preamble(fingerprint, config)
    assert _AI_VOCAB.description in _avoid_block(preamble)


def test_never_legitimate_tell_is_never_genuine() -> None:
    """A margin-0 tell (never legitimate) is not excludable even if measured."""
    config = AIStyleConfig()
    cutoff = TELL_REGISTRY["knowledge_cutoff"]
    assert cutoff.margin == 0.0
    fingerprint = _fingerprint({cutoff.feature_key: 50.0})
    preamble = build_style_preamble(fingerprint, config)
    assert cutoff.description in _avoid_block(preamble)


def test_thin_fingerprint_falls_back_to_generic() -> None:
    """A thin fingerprint skips measured targets and notes it is preliminary."""
    config = AIStyleConfig()
    thin = VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=4.2, support=2)},
        fragment_count=2,
    )
    preamble = build_style_preamble(thin, config)
    assert "preliminary" in preamble.lower()
    # No personalised measured target rendered from the untrusted rate.
    assert "4.2" not in preamble
    # Generic avoids still present.
    assert _AI_VOCAB.description in _avoid_block(preamble)


def test_thin_fingerprint_does_not_exclude_genuine() -> None:
    """When thin, even a high measured rate cannot remove an avoid (untrusted)."""
    config = AIStyleConfig()
    thin = VoiceFingerprint(
        features={_AI_VOCAB.feature_key: FeatureStat(rate=12.0, support=2)},
        fragment_count=2,
    )
    preamble = build_style_preamble(thin, config)
    assert _AI_VOCAB.description in _avoid_block(preamble)


def test_disabled_targets_still_personalise_avoids() -> None:
    """Disabling measured targets removes the positive block but still personalises."""
    config = AIStyleConfig(include_measured_targets=False)
    fingerprint = _fingerprint({"em_dash_density": 4.2, _AI_VOCAB.feature_key: 12.0})
    preamble = build_style_preamble(fingerprint, config)
    # No positive measured target rendered.
    assert "4.2" not in preamble
    # Avoid-list personalisation still applies (genuine feature excluded).
    assert _AI_VOCAB.description not in _avoid_block(preamble)


def test_length_capped() -> None:
    """A small cap trims the avoid list but keeps the header and targets."""
    cap = 320
    config = AIStyleConfig(preamble_max_chars=cap)
    preamble = build_style_preamble(_fingerprint({"em_dash_density": 4.2}), config)
    assert len(preamble) <= cap
    assert "## Voice targets" in preamble
    assert "em-dash" in preamble
    # The cap actually bit: not every registry avoid fits.
    avoid_count = sum(1 for t in TELL_REGISTRY.values() if t.polarity == "avoid")
    rendered = preamble.count("\n- ")
    assert rendered < avoid_count


def test_zero_cap_is_unlimited() -> None:
    """A cap of 0 imposes no limit (all avoids rendered)."""
    config = AIStyleConfig(preamble_max_chars=0)
    preamble = build_style_preamble(_fingerprint({"em_dash_density": 4.2}), config)
    assert _AI_VOCAB.description in preamble


def _avoid_block(preamble: str) -> str:
    """Return only the avoid section of *preamble* (after the avoid lead)."""
    marker = "Avoid drifting"
    idx = preamble.find(marker)
    return "" if idx < 0 else preamble[idx:]
