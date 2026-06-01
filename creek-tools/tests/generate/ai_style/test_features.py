"""Tests for the fingerprint feature extractors (FEAT-040.2)."""

from __future__ import annotations

import pytest

from creek.generate.ai_style.features import (
    FINGERPRINT_FEATURES,
    ai_vocab_density,
    concrete_density,
    curly_quote_density,
    em_dash_density,
    marketing_verb_ratio,
    rule_of_three_rate,
    sentence_length_mean,
    transition_opener_rate,
)


def test_em_dash_density_normalised_by_length() -> None:
    """Two em-dashes in a thousand-word text is a rate of ~2."""
    text = "— — " + " ".join(["word"] * 998)
    assert em_dash_density(text) == pytest.approx(2.0, abs=0.05)


def test_curly_quote_density_counts_all_directional_marks() -> None:
    """Curly quotes and apostrophes both count."""
    assert curly_quote_density("\u201cit\u2019s\u201d said \u2018x\u2019") > 0.0
    assert curly_quote_density('"straight" only') == 0.0


def test_ai_vocab_density_counts_listed_words() -> None:
    """Listed AI-vocabulary words are counted; ordinary words are not."""
    assert ai_vocab_density("a vibrant tapestry to delve into") > 0.0
    assert ai_vocab_density("a plain sentence about rivers") == 0.0


class TestMarketingVerbRatio:
    """Marketing verbs relative to plain copulas."""

    def test_ratio_balances_marketing_and_copula(self) -> None:
        """One marketing verb and one copula gives 0.5."""
        assert marketing_verb_ratio("It is here. It boasts a pool.") == pytest.approx(
            0.5,
        )

    def test_zero_when_neither_present(self) -> None:
        """No verbs of either kind yields 0.0, not a divide-by-zero."""
        assert marketing_verb_ratio("rivers flow downhill") == 0.0


class TestSentenceLengthMean:
    """Mean words per sentence."""

    def test_mean_across_sentences(self) -> None:
        """`One two three. Four five.` averages 2.5 words."""
        assert sentence_length_mean("One two three. Four five.") == pytest.approx(2.5)

    def test_no_terminator_is_single_sentence(self) -> None:
        """Terminator-free text counts as one sentence of all its words."""
        assert sentence_length_mean("one two three four") == pytest.approx(4.0)


def test_transition_opener_rate_counts_sentence_initial() -> None:
    """Only sentence-initial transition words count."""
    text = "Additionally, this. Moreover, that. The river flowed on."
    assert transition_opener_rate(text) > 0.0
    assert transition_opener_rate("The river flowed on quietly.") == 0.0


def test_rule_of_three_rate_detects_triads() -> None:
    """An `a, b, and c` triad is detected; a pair is not."""
    assert rule_of_three_rate("red, white, and blue") > 0.0
    assert rule_of_three_rate("salt and pepper") == 0.0


def test_concrete_density_counts_the_word() -> None:
    """ "concrete" is counted; absent text scores zero."""
    assert concrete_density("no concrete evidence and concrete examples") > 0.0
    assert concrete_density("a plain sentence") == 0.0


def test_registry_exposes_all_extractors() -> None:
    """Every extractor is registered under a stable key."""
    assert "em_dash_density" in FINGERPRINT_FEATURES
    assert FINGERPRINT_FEATURES["ai_vocab_density"] is ai_vocab_density
    assert FINGERPRINT_FEATURES["concrete_density"] is concrete_density
    assert len(FINGERPRINT_FEATURES) == 18
