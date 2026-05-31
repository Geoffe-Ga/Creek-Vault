"""Tests for the scan engine and tell registry (FEAT-040.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.tells import (
    TELL_REGISTRY,
    Span,
    Tell,
    get_tells,
    rate_per_kwords,
    register,
    word_count,
)

_CLEAN = "I went to the river and watched the water move for a while. It was good."
_PLACEHOLDER = "Retrieved on 2025-xx-xx from the official site; see also 2024-XX-XX."


@pytest.fixture
def empty_fingerprint() -> VoiceFingerprint:
    """A non-thin but featureless fingerprint (forces generic priors)."""
    return VoiceFingerprint(features={}, fragment_count=50)


class TestHelpers:
    """Word counting and rate normalisation."""

    def test_word_count_minimum_one(self) -> None:
        """Empty/whitespace text counts as one word, never zero."""
        assert word_count("") == 1
        assert word_count("   \n ") == 1

    def test_word_count_counts_tokens(self) -> None:
        """Word tokens are counted by the word-boundary regex."""
        assert word_count("two words") == 2

    def test_rate_per_kwords_normalises_by_length(self) -> None:
        """The same count is a higher rate in a shorter text."""
        short = rate_per_kwords(1, "a b c d e")  # 5 words
        long = rate_per_kwords(1, " ".join(["w"] * 1000))
        assert short > long
        assert long == pytest.approx(1.0)


class TestScanSeedTell:
    """The placeholder-date seed tell drives end-to-end behaviour."""

    def test_clean_text_no_findings_zero_distance(
        self,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """Clean prose against a silent fingerprint flags nothing."""
        report = scan(_CLEAN, fingerprint=empty_fingerprint, config=AIStyleConfig())
        assert report.findings == []
        assert report.voice_distance == 0.0

    def test_placeholder_flags_with_spans(
        self,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """Each placeholder date produces an over-use finding with a span."""
        report = scan(
            _PLACEHOLDER,
            fingerprint=empty_fingerprint,
            config=AIStyleConfig(),
        )
        assert len(report.findings) == 2
        first = report.findings[0]
        assert first.tell_id == "placeholder_date"
        assert first.direction == "over"
        assert first.span.end > first.span.start
        assert first.line == 1
        assert "2025-xx-xx" in first.excerpt
        assert report.voice_distance > 0.0

    def test_disabled_config_returns_empty(
        self,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """The master switch short-circuits the scan."""
        report = scan(
            _PLACEHOLDER,
            fingerprint=empty_fingerprint,
            config=AIStyleConfig(enabled=False),
        )
        assert report.findings == []
        assert report.voice_distance == 0.0

    def test_category_toggle_skips_family(
        self,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """Disabling the mechanical category drops the seed tell."""
        config = AIStyleConfig(enabled_categories=["lexical"])
        report = scan(_PLACEHOLDER, fingerprint=empty_fingerprint, config=config)
        assert report.findings == []

    def test_line_number_tracks_newlines(
        self,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """A finding reports the 1-based line of its span."""
        text = "first line\nsecond line\nsee 2025-xx-xx here"
        report = scan(text, fingerprint=empty_fingerprint, config=AIStyleConfig())
        assert report.findings[0].line == 3


class TestVaultRelativeSuppression:
    """A feature the user genuinely uses must not flag."""

    def test_user_rate_above_draft_suppresses_finding(self) -> None:
        """When the user's stored rate exceeds the draft's, nothing fires."""
        # Absurd but exercises the path: a user who "writes" placeholder dates
        # far more than this draft does ⇒ draft is under the baseline ⇒ no flag.
        fingerprint = VoiceFingerprint(
            features={"placeholder_date_rate": FeatureStat(rate=999.0, support=50)},
            fragment_count=50,
        )
        report = scan(_PLACEHOLDER, fingerprint=fingerprint, config=AIStyleConfig())
        assert report.findings == []
        assert report.voice_distance == 0.0


class TestThinFingerprint:
    """Sparse corpora soften flagging instead of over-reacting."""

    def test_thin_flag_and_softened_distance(self) -> None:
        """A thin fingerprint sets the flag and lowers the distance."""
        thin = VoiceFingerprint(features={}, fragment_count=1)
        full = VoiceFingerprint(features={}, fragment_count=50)
        config = AIStyleConfig()
        thin_report = scan(_PLACEHOLDER, fingerprint=thin, config=config)
        full_report = scan(_PLACEHOLDER, fingerprint=full, config=config)
        assert thin_report.thin_fingerprint is True
        assert full_report.thin_fingerprint is False
        assert 0.0 < thin_report.voice_distance < full_report.voice_distance


@pytest.fixture
def comment_only_tell() -> Iterator[Tell]:
    """Register a temporary comment-only tell, then remove it."""
    tell = Tell(
        id="_test_comment_only",
        category="discourse",
        feature_key="_test_comment_only_rate",
        handling="surface",
        polarity="avoid",
        description="test-only comment tell",
        caveat="test fixture",
        measure=lambda text: rate_per_kwords(text.count("CERTAINLY"), text),
        locate=lambda _text: [],
        contexts=frozenset({"comment"}),
    )
    register(tell)
    try:
        yield tell
    finally:
        TELL_REGISTRY.pop(tell.id, None)


class TestContextFiltering:
    """Context-restricted tells only run in their declared contexts."""

    def test_comment_tell_skipped_in_article(
        self,
        comment_only_tell: Tell,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """A comment-only tell does not fire when scanning article text."""
        assert comment_only_tell.applies_in("article") is False
        report = scan(
            "CERTAINLY CERTAINLY",
            fingerprint=empty_fingerprint,
            config=AIStyleConfig(),
            context="article",
        )
        assert all(f.tell_id != "_test_comment_only" for f in report.findings)

    def test_comment_tell_fires_in_comment(
        self,
        comment_only_tell: Tell,
        empty_fingerprint: VoiceFingerprint,
    ) -> None:
        """The same tell fires when scanning comment text."""
        report = scan(
            "CERTAINLY CERTAINLY CERTAINLY",
            fingerprint=empty_fingerprint,
            config=AIStyleConfig(),
            context="comment",
        )
        assert any(f.tell_id == "_test_comment_only" for f in report.findings)


class TestRegistry:
    """Registry behaviour."""

    def test_get_tells_filters_by_category(self) -> None:
        """The seed tell is in the mechanical family only."""
        assert any(t.id == "placeholder_date" for t in get_tells(["mechanical"]))
        assert all(t.id != "placeholder_date" for t in get_tells(["lexical"]))

    def test_duplicate_registration_raises(self) -> None:
        """Re-registering an existing id is a programming error."""
        existing = TELL_REGISTRY["placeholder_date"]
        with pytest.raises(ValueError, match="duplicate tell id"):
            register(existing)


def test_span_is_hashable_range() -> None:
    """Span is a frozen [start, end) value object."""
    span = Span(2, 5)
    assert (span.start, span.end) == (2, 5)
