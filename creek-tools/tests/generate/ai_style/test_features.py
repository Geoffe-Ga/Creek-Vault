"""Tests for the fingerprint feature extractors (FEAT-040.2)."""

from __future__ import annotations

import pytest

from creek.generate.ai_style.features import (
    FINGERPRINT_FEATURES,
    NEGATIVE_PARALLELISM_RE,
    ai_vocab_density,
    concrete_density,
    curly_quote_density,
    em_dash_density,
    marketing_verb_ratio,
    negative_parallelism_density,
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


class TestNegativeParallelismRegex:
    """The broadened antithesis pattern (issue #516)."""

    # Existing constructions must keep matching. The contracted
    # `it's not X, it's Y` family was matched by the original regex, so it
    # belongs here, not in the broadened-pattern fixtures.
    _LEGACY = (
        "It's not only fast, but also cheap.",
        "It's not a bug, it's a feature.",
        "No gods, no masters, just us.",
        "It's not about the money, it's about the principle.",
    )
    # The bare `not X, but Y` family and cross-sentence subject restatement
    # that the old pattern missed (reproduction sentences from issue #516).
    _NEW = (
        "Not through bliss, necessarily, but through outrage, the sticky ones.",
        "We win not through force but through patience.",
        "The parables weren't instruction manuals. They were field reports.",
        "The work isn't to save everyone. The work is to be findable.",
        "It doesn't make you easier to manipulate—it makes you ungovernable.",
        "It is not about winning, it is about showing up.",
    )

    @pytest.mark.parametrize("text", _LEGACY)
    def test_legacy_constructions_still_match(self, text: str) -> None:
        """The constructions the original regex caught keep firing."""
        assert NEGATIVE_PARALLELISM_RE.search(text) is not None

    @pytest.mark.parametrize("text", _NEW)
    def test_new_constructions_match(self, text: str) -> None:
        """Each issue #516 reproduction sentence is now matched."""
        assert NEGATIVE_PARALLELISM_RE.search(text) is not None

    def test_ordinary_sentence_does_not_match(self) -> None:
        """A plain sentence with no antithesis must not match."""
        plain = "The river flowed quietly downhill."
        assert NEGATIVE_PARALLELISM_RE.search(plain) is None

    def test_plain_negation_alone_does_not_match(self) -> None:
        """A bare negation with no contrasting clause must not match."""
        assert NEGATIVE_PARALLELISM_RE.search("I do not like the rain at all.") is None

    def test_cross_sentence_different_subjects_does_not_match(self) -> None:
        """A negation sentence followed by an unrelated `The <word>` is not antithesis.

        The cross-sentence arm must only fire on a genuinely repeated subject
        (a pronoun echo or the same noun restated), not on any new `The <word>`.
        """
        pair = "The algorithm doesn't converge. The teacher is patient."
        assert NEGATIVE_PARALLELISM_RE.search(pair) is None

    def test_casual_concessive_does_not_match(self) -> None:
        """A casual concessive (`Not X but Y`, no shared preposition) must not flag.

        Pins the boundary the PR body claims: the shared-preposition design
        keeps a concessive aside out of the antithesis count.
        """
        assert (
            NEGATIVE_PARALLELISM_RE.search("Not my best work but it is honest") is None
        )

    def test_different_prepositions_does_not_match(self) -> None:
        """`not <prep> X but <prep'> Y` with DIFFERENT prepositions must not flag.

        The bare ``not <prep> … but <prep>`` arm requires the SAME preposition
        on both sides via a backreference — that shared preposition is what
        makes the construction parallel antithesis rather than a casual aside.
        Here ``through`` and ``with`` differ, so the arm must not fire.
        """
        assert (
            NEGATIVE_PARALLELISM_RE.search("He gave not through duty but with love.")
            is None
        )

    def test_pronoun_to_pronoun_disagreement_does_not_match(self) -> None:
        """A bare-pronoun first subject echoed by a different pronoun is not antithesis.

        "She isn't happy. They are sad." pairs unrelated referents: the cross-
        sentence arm requires the first clause to carry a concrete ``the <noun>``
        subject, so a pronoun-led first clause cannot anchor a pronoun echo.
        This excludes the over-match while keeping the noun→pronoun echo
        ("The parables … They") and repeated-noun ("The work … The work") paths.
        """
        pair = "She isn't happy. They are sad."
        assert NEGATIVE_PARALLELISM_RE.search(pair) is None


class TestNegativeParallelismDensity:
    """The density extractor over the broadened pattern (issue #516)."""

    _SATURATED = (
        "Not through bliss, necessarily, but through outrage. "
        "The parables weren't instruction manuals. They were field reports. "
        "The work isn't to save everyone. The work is to be findable. "
        "It doesn't make you easier to manipulate—it makes you ungovernable. "
        "It is not about winning, it is about showing up."
    )
    _CONTROL = "The river flowed downhill. " + " ".join(["plain"] * 40)

    def test_density_higher_on_saturated_than_clean(self) -> None:
        """A paragraph dense with antithesis scores well above a clean control."""
        saturated = negative_parallelism_density(self._SATURATED)
        clean = negative_parallelism_density(self._CONTROL)
        assert saturated > clean
        # Magnitude floor so a silent drop in match count is caught. The
        # saturated fixture currently scores ~76.9 per 1000 words (5 matches /
        # 65 words); 30.0 leaves generous headroom while still flagging a
        # regression that loses several of those matches.
        assert saturated > 30.0

    def test_clean_control_is_zero(self) -> None:
        """The clean control has no antithesis density."""
        assert negative_parallelism_density(self._CONTROL) == 0.0


def test_registry_exposes_all_extractors() -> None:
    """Every extractor is registered under a stable key."""
    assert "em_dash_density" in FINGERPRINT_FEATURES
    assert FINGERPRINT_FEATURES["ai_vocab_density"] is ai_vocab_density
    assert FINGERPRINT_FEATURES["concrete_density"] is concrete_density
    assert len(FINGERPRINT_FEATURES) == 19
