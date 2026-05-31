"""Tests for the vault-relative lexical detectors (FEAT-040.5)."""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.lexical import (
    _locate_ai_vocab,
    _locate_marketing,
    _locate_transitions,
)
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.tells import get_tells

_CONFIG = AIStyleConfig()

# A user who uses none of these constructions.
_PLAIN = VoiceFingerprint(
    features={
        "ai_vocab_density": FeatureStat(rate=0.0, support=50),
        "marketing_verb_ratio": FeatureStat(rate=0.0, support=50),
        "transition_opener_rate": FeatureStat(rate=0.0, support=50),
        "concrete_rate": FeatureStat(rate=0.0, support=50),
    },
    fragment_count=50,
)
# A user who genuinely writes this way (very high baselines).
_ORNATE = VoiceFingerprint(
    features={
        "ai_vocab_density": FeatureStat(rate=900.0, support=50),
        "marketing_verb_ratio": FeatureStat(rate=0.99, support=50),
        "transition_opener_rate": FeatureStat(rate=900.0, support=50),
        "concrete_rate": FeatureStat(rate=900.0, support=50),
    },
    fragment_count=50,
)


def _fired(report, tell_id: str) -> bool:
    return any(f.tell_id == tell_id for f in report.findings)


def test_lexical_tells_registered() -> None:
    """All four lexical tells are in the registry under the lexical family."""
    ids = {t.id for t in get_tells(["lexical"])}
    assert {
        "ai_vocabulary",
        "copulative_avoidance",
        "transition_opener",
        "concrete_comment",
    } <= ids


class TestVaultRelative:
    """Each tell fires above the user's baseline and is suppressed at/below."""

    def test_ai_vocab_over_baseline_fires(self) -> None:
        """AI-vocab-dense text flags for a user who avoids those words."""
        text = "a vibrant tapestry that delve into the intricate, pivotal core"
        assert _fired(scan(text, fingerprint=_PLAIN, config=_CONFIG), "ai_vocabulary")

    def test_ai_vocab_suppressed_for_ornate_user(self) -> None:
        """The same text does not flag for a user who writes that way."""
        text = "a vibrant tapestry that delve into the intricate, pivotal core"
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "ai_vocabulary",
        )

    def test_marketing_verbs_over_baseline_fires(self) -> None:
        """Marketing-verb prose flags for a plain user."""
        text = "The hall serves as a hub. It boasts a pool and offers views."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG),
            "copulative_avoidance",
        )

    def test_marketing_verbs_suppressed_for_ornate_user(self) -> None:
        """The same prose is voice, not a tell, for an ornate user."""
        text = "The hall serves as a hub. It boasts a pool and offers views."
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "copulative_avoidance",
        )

    def test_transition_openers_fire_then_suppress(self) -> None:
        """Sentence-initial transitions flag for plain, not for ornate."""
        text = "Additionally, this. Moreover, that. Furthermore, more."
        assert _fired(
            scan(text, fingerprint=_PLAIN, config=_CONFIG),
            "transition_opener",
        )
        assert not _fired(
            scan(text, fingerprint=_ORNATE, config=_CONFIG),
            "transition_opener",
        )


class TestConcreteContext:
    """The 'concrete' tell is comment-context only."""

    def test_not_in_article(self) -> None:
        """'concrete' in article prose does not fire."""
        report = scan(
            "There is no concrete evidence of that yet.",
            fingerprint=_PLAIN,
            config=_CONFIG,
            context="article",
        )
        assert not _fired(report, "concrete_comment")

    def test_fires_in_comment(self) -> None:
        """'concrete' fires in comment context for a user who avoids it."""
        report = scan(
            "Without concrete evidence and concrete examples, this fails.",
            fingerprint=_PLAIN,
            config=_CONFIG,
            context="comment",
        )
        assert _fired(report, "concrete_comment")

    def test_suppressed_for_concrete_using_user(self) -> None:
        """The same comment is suppressed when the user's baseline is high."""
        report = scan(
            "Without concrete evidence and concrete examples, this fails.",
            fingerprint=_ORNATE,
            config=_CONFIG,
            context="comment",
        )
        assert not _fired(report, "concrete_comment")


class TestLocators:
    """Span locators point at the offending text."""

    def test_locate_ai_vocab(self) -> None:
        """AI-vocab words are located."""
        spans = _locate_ai_vocab("a tapestry and a delve")
        assert len(spans) == 2

    def test_locate_marketing(self) -> None:
        """Marketing verbs are located."""
        assert _locate_marketing("it boasts and serves as") != []

    def test_locate_transitions_sentence_initial_only(self) -> None:
        """Only sentence-initial transitions are located."""
        spans = _locate_transitions("Additionally, yes. I add moreover mid-sentence.")
        assert len(spans) == 1
        text = "Additionally, yes. I add moreover mid-sentence."
        assert text[spans[0].start : spans[0].end].lower() == "additionally"


def test_one_ai_vocab_word_below_margin_no_false_positive() -> None:
    """A single AI-vocab word in a long human text stays under the margin."""
    text = "robust " + " ".join(["plain"] * 999)
    assert not _fired(scan(text, fingerprint=_PLAIN, config=_CONFIG), "ai_vocabulary")
