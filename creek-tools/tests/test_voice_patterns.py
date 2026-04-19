"""Tests for creek.generate.voice — voice pattern extraction.

Covers the ``VoicePatternExtractor`` class implementing Section 11.1 of
the Creek Ontology: extract sentence structure, paragraph structure,
metaphor families, rhetorical moves, vocabulary fingerprint, and
punctuation habits from voice exemplar texts.
"""

from __future__ import annotations

import pytest

from creek.generate.voice import (
    METAPHOR_DOMAINS,
    MetaphorFamily,
    ParagraphMetrics,
    PunctuationHabits,
    RhetoricalMoves,
    SentenceMetrics,
    VocabularyFingerprint,
    VoicePatternExtractor,
    VoicePatterns,
)

# ---- Fixtures ----


@pytest.fixture()
def extractor() -> VoicePatternExtractor:
    """Create a default VoicePatternExtractor."""
    return VoicePatternExtractor()


@pytest.fixture()
def sample_texts() -> list[str]:
    """Known voice sample texts for deterministic testing."""
    return [
        (
            "The pattern emerges clearly when we examine the data. "
            "Short sentences work. "
            "But longer sentences, those that wind through multiple "
            "clauses and subordinate ideas, reveal the underlying "
            "complexity of the analysis.\n\n"
            "This is probably not the most elegant way to put it, "
            "but the creek of thought flows where it will. "
            "The current carries meaning downstream. "
            "Each eddy pools around a central insight.\n\n"
            "As I mentioned earlier, the paradox is this: simplicity "
            "requires complexity. And yet, the reverse is also true."
        ),
        (
            "Is this what illumination feels like? "
            "The bright glow of understanding... "
            "I'm not sure if I'm overthinking this, "
            "but the spark ignites something deeper -- "
            "a flame that burns through pretense.\n\n"
            "Going back to the original question, "
            "we find (perhaps unsurprisingly) that the answer "
            "was hiding in plain sight!"
        ),
    ]


# ---- Module surface ----


class TestModuleSurface:
    """Exported names and constants for voice pattern extraction."""

    def test_metaphor_domains_has_expected_keys(self) -> None:
        """METAPHOR_DOMAINS must include the ontology-specified domains."""
        expected = {"water", "light", "fire", "body"}
        assert expected.issubset(set(METAPHOR_DOMAINS))

    def test_metaphor_domains_water_keywords(self) -> None:
        """Water domain must include the keywords from the issue spec."""
        water = METAPHOR_DOMAINS["water"]
        for word in ("creek", "flow", "pool", "eddy", "current", "stream", "wave"):
            assert word in water

    def test_metaphor_domains_light_keywords(self) -> None:
        """Light domain must include the keywords from the issue spec."""
        light = METAPHOR_DOMAINS["light"]
        for word in ("illuminate", "shadow", "dark", "bright", "glow"):
            assert word in light


# ---- extract_patterns (integration) ----


class TestExtractPatterns:
    """Top-level extract_patterns returns a complete VoicePatterns."""

    def test_returns_voice_patterns_type(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """extract_patterns must return a VoicePatterns instance."""
        result = extractor.extract_patterns(sample_texts)
        assert isinstance(result, VoicePatterns)

    def test_all_fields_populated(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """Every field of VoicePatterns must be set."""
        result = extractor.extract_patterns(sample_texts)
        assert isinstance(result.sentence_metrics, SentenceMetrics)
        assert isinstance(result.paragraph_metrics, ParagraphMetrics)
        assert isinstance(result.transitions, tuple)
        assert isinstance(result.metaphor_families, tuple)
        assert isinstance(result.rhetorical_moves, RhetoricalMoves)
        assert isinstance(result.vocabulary, VocabularyFingerprint)
        assert isinstance(result.punctuation, PunctuationHabits)

    def test_empty_input_returns_zeroed_patterns(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Empty text list produces zeroed-out metrics."""
        result = extractor.extract_patterns([])
        assert result.sentence_metrics.total_sentences == 0
        assert result.sentence_metrics.average_length == 0.0
        assert result.paragraph_metrics.average_sentences_per_paragraph == 0.0


# ---- Sentence metrics ----


class TestSentenceMetrics:
    """Sentence structure: length, distribution, fragments, questions."""

    def test_average_sentence_length(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Average length is the mean word count across all sentences."""
        texts = ["I run. I walk quickly. I eat my food every single day."]
        result = extractor.extract_patterns(texts)
        # 3 sentences: 2 words, 3 words, 7 words => avg 4.0
        assert result.sentence_metrics.average_length == pytest.approx(4.0)

    def test_sentence_length_distribution(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Short (<10), medium (10-25), and long (>25) counts are correct."""
        texts = [
            "Short. "
            "This is a medium length sentence with exactly enough words here now. "
            "This is a very long sentence that deliberately contains more than "
            "twenty five words so that it can be classified as a long sentence "
            "in the distribution bucket."
        ]
        result = extractor.extract_patterns(texts)
        assert result.sentence_metrics.short_count >= 1
        assert result.sentence_metrics.long_count >= 1

    def test_question_frequency(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Question frequency is the fraction of sentences ending with '?'."""
        texts = ["Is this a question? Yes it is. Another question? No."]
        result = extractor.extract_patterns(texts)
        # 4 sentences, 2 questions => 0.5
        assert result.sentence_metrics.question_frequency == pytest.approx(0.5)

    def test_fragment_detection(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Very short sentences (1-3 words) are counted as fragments."""
        texts = ["Indeed. The analysis reveals deep patterns. Yes. Absolutely."]
        result = extractor.extract_patterns(texts)
        # "Indeed.", "Yes.", "Absolutely." are fragments (1 word each)
        assert result.sentence_metrics.fragment_count >= 3

    def test_total_sentences_counted(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Total sentence count is accurate."""
        texts = ["One. Two. Three."]
        result = extractor.extract_patterns(texts)
        assert result.sentence_metrics.total_sentences == 3


# ---- Paragraph metrics ----


class TestParagraphMetrics:
    """Paragraph structure: average sentences per paragraph."""

    def test_average_sentences_per_paragraph(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Average sentences per paragraph is computed correctly."""
        texts = ["First sentence. Second sentence.\n\nThird sentence."]
        result = extractor.extract_patterns(texts)
        # 2 paragraphs: [2 sentences, 1 sentence] => avg 1.5
        avg = result.paragraph_metrics.average_sentences_per_paragraph
        assert avg == pytest.approx(1.5)

    def test_single_paragraph(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """A single paragraph with multiple sentences works correctly."""
        texts = ["First. Second. Third."]
        result = extractor.extract_patterns(texts)
        avg = result.paragraph_metrics.average_sentences_per_paragraph
        assert avg == pytest.approx(3.0)


# ---- Transition patterns ----


class TestTransitionPatterns:
    """Transition words and phrases detected in text."""

    def test_detects_common_transitions(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Common transition words like 'however' and 'therefore' are found."""
        texts = [
            "The data is clear. However, there are caveats. "
            "Therefore, we must be careful. Moreover, the sample is small."
        ]
        result = extractor.extract_patterns(texts)
        transition_words = {t[0] for t in result.transitions}
        assert "however" in transition_words
        assert "therefore" in transition_words

    def test_transitions_sorted_by_count(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Transitions are sorted by descending count."""
        texts = ["However, this. However, that. However, again. Therefore, once."]
        result = extractor.extract_patterns(texts)
        if len(result.transitions) >= 2:
            assert result.transitions[0][1] >= result.transitions[1][1]


# ---- Metaphor families ----


class TestMetaphorFamilies:
    """Metaphor family detection from keyword lists."""

    def test_detects_water_metaphors(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Water domain keywords trigger a MetaphorFamily detection."""
        texts = ["The creek of thought flows into a pool of insight."]
        families = extractor.extract_metaphors(texts)
        domains = {f.domain for f in families}
        assert "water" in domains

    def test_water_family_has_matched_keywords(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """The water family includes the specific keywords found."""
        texts = ["The creek flows downstream into a deep eddy."]
        families = extractor.extract_metaphors(texts)
        water = next(f for f in families if f.domain == "water")
        assert "creek" in water.matches
        assert "eddy" in water.matches

    def test_detects_light_metaphors(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Light domain keywords trigger detection."""
        texts = ["The bright glow of understanding illuminates the shadow."]
        families = extractor.extract_metaphors(texts)
        domains = {f.domain for f in families}
        assert "light" in domains

    def test_detects_fire_metaphors(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Fire domain keywords trigger detection."""
        texts = ["The spark ignites a flame that burns through pretense."]
        families = extractor.extract_metaphors(texts)
        domains = {f.domain for f in families}
        assert "fire" in domains

    def test_no_metaphors_in_plain_text(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Plain text without metaphor keywords returns no families."""
        texts = ["The report was submitted on Tuesday."]
        families = extractor.extract_metaphors(texts)
        assert families == []

    def test_example_sentences_included(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Each MetaphorFamily includes the sentences where matches appeared."""
        texts = ["The creek flows gently. Nothing here. The eddy swirls round."]
        families = extractor.extract_metaphors(texts)
        water = next(f for f in families if f.domain == "water")
        assert len(water.example_sentences) >= 1

    def test_returns_metaphor_family_type(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """extract_metaphors returns a list of MetaphorFamily."""
        texts = ["The creek flows here."]
        families = extractor.extract_metaphors(texts)
        assert all(isinstance(f, MetaphorFamily) for f in families)

    def test_empty_input_returns_empty(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Empty text list returns no metaphor families."""
        assert extractor.extract_metaphors([]) == []


# ---- Rhetorical moves ----


class TestRhetoricalMoves:
    """Self-deprecation, paradox construction, callback references."""

    def test_detects_self_deprecation(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Self-deprecating phrases before insights are counted."""
        texts = [
            "This is probably not the best way to say it, but the data is clear. "
            "I'm not sure if this makes sense, but here goes."
        ]
        result = extractor.extract_patterns(texts)
        assert result.rhetorical_moves.self_deprecation_count >= 2

    def test_detects_paradox_construction(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Paradox phrases like 'and yet' are counted."""
        texts = [
            "Simplicity requires complexity. And yet, the reverse is true. "
            "But also, nothing is ever that simple."
        ]
        result = extractor.extract_patterns(texts)
        assert result.rhetorical_moves.paradox_count >= 2

    def test_detects_callback_references(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Callback phrases like 'as I mentioned' are counted."""
        texts = [
            "As I mentioned earlier, this matters. "
            "Going back to the original point, we see clarity."
        ]
        result = extractor.extract_patterns(texts)
        assert result.rhetorical_moves.callback_count >= 2

    def test_no_moves_in_plain_text(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Plain text without rhetorical patterns returns zero counts."""
        texts = ["The cat sat on the mat."]
        result = extractor.extract_patterns(texts)
        moves = result.rhetorical_moves
        assert moves.self_deprecation_count == 0
        assert moves.paradox_count == 0
        assert moves.callback_count == 0


# ---- Vocabulary fingerprint ----


class TestVocabulary:
    """TF-IDF distinctive words, coined terms, recurring phrases."""

    def test_returns_vocabulary_fingerprint_type(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """extract_vocabulary returns a VocabularyFingerprint."""
        texts = ["The creek flows gently downstream."]
        result = extractor.extract_vocabulary(texts)
        assert isinstance(result, VocabularyFingerprint)

    def test_distinctive_words_found(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Distinctive words are identified from the text corpus."""
        texts = [
            "The ontology provides a framework for knowledge organization.",
            "Knowledge organization requires careful ontological analysis.",
            "The cat sat on the mat near the hat.",
        ]
        result = extractor.extract_vocabulary(texts)
        assert len(result.distinctive_words) > 0

    def test_distinctive_words_single_document(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Single-document corpus ranks distinctive words by frequency.

        Without IDF smoothing, ``log(n_docs / doc_freq) == log(1/1) == 0``
        for every term, so all TF-IDF scores collapse to zero and the
        returned ordering reflects dict insertion order rather than
        actual distinctiveness. This test exercises the smoothed branch.
        """
        # ``insight`` appears 3 times near the end; earlier words appear once.
        result = extractor.extract_vocabulary(
            [
                "apple banana cherry date elderberry fig grape honeydew "
                "iced jackfruit insight insight insight.",
            ],
        )
        assert len(result.distinctive_words) > 0
        # With smoothed IDF, ``insight`` (term frequency 3) ranks ahead of
        # the single-occurrence tokens. Without smoothing it would not
        # appear in the top three (it is 11th in insertion order).
        assert "insight" in result.distinctive_words[:3]

    def test_recurring_phrases_detected(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Phrases appearing multiple times across texts are captured."""
        texts = [
            "The creek of thought flows here. The creek of thought returns.",
            "Once more the creek of thought appears in writing.",
        ]
        result = extractor.extract_vocabulary(texts)
        assert len(result.recurring_phrases) > 0

    def test_coined_terms_with_reference_words(self) -> None:
        """Words not in the reference set are flagged as coined terms."""
        reference = frozenset({"the", "cat", "sat", "on", "mat"})
        ext = VoicePatternExtractor(reference_words=reference)
        texts = ["The cat sat on the mat with a zorblax nearby."]
        result = ext.extract_vocabulary(texts)
        assert "zorblax" in result.coined_terms

    def test_no_coined_terms_without_reference(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Without a reference word set, coined terms is empty."""
        texts = ["The cat sat on the mat."]
        result = extractor.extract_vocabulary(texts)
        assert result.coined_terms == ()

    def test_empty_input_returns_empty_vocabulary_fingerprint(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Empty texts produce an empty VocabularyFingerprint."""
        result = extractor.extract_vocabulary([])
        assert result.distinctive_words == ()
        assert result.coined_terms == ()
        assert result.recurring_phrases == ()


# ---- Punctuation habits ----


class TestPunctuationHabits:
    """Em-dash, ellipsis, parenthetical, and exclamation mark frequency."""

    def test_em_dash_detection(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Em-dashes (-- or unicode) are counted per 1000 words."""
        # 10 words + 1 em-dash = frequency of 100.0 per 1000 words
        texts = ["This is a sentence -- with an em-dash in the middle here."]
        result = extractor.extract_patterns(texts)
        assert result.punctuation.em_dash_frequency > 0.0

    def test_ellipsis_detection(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Ellipses (... or unicode) are counted per 1000 words."""
        texts = ["The thought trails off... and then returns."]
        result = extractor.extract_patterns(texts)
        assert result.punctuation.ellipsis_frequency > 0.0

    def test_parenthetical_detection(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Parenthetical asides are counted per 1000 words."""
        texts = ["The answer (perhaps unsurprisingly) was obvious."]
        result = extractor.extract_patterns(texts)
        assert result.punctuation.parenthetical_frequency > 0.0

    def test_exclamation_detection(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Exclamation marks are counted per 1000 words."""
        texts = ["What a revelation! Amazing! The data confirms it."]
        result = extractor.extract_patterns(texts)
        assert result.punctuation.exclamation_frequency > 0.0

    def test_no_punctuation_returns_zero(
        self,
        extractor: VoicePatternExtractor,
    ) -> None:
        """Plain text without special punctuation returns zero frequencies."""
        texts = ["The cat sat on the mat."]
        result = extractor.extract_patterns(texts)
        assert result.punctuation.em_dash_frequency == 0.0
        assert result.punctuation.ellipsis_frequency == 0.0
        assert result.punctuation.parenthetical_frequency == 0.0
        assert result.punctuation.exclamation_frequency == 0.0


# ---- Integration with sample texts ----


class TestIntegrationWithSamples:
    """End-to-end tests using the sample voice texts."""

    def test_sample_texts_detect_water_metaphors(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """The sample texts contain water metaphors (creek, eddy, flows)."""
        result = extractor.extract_patterns(sample_texts)
        domains = {f.domain for f in result.metaphor_families}
        assert "water" in domains

    def test_sample_texts_detect_light_metaphors(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """The sample texts contain light metaphors (illumination, glow)."""
        result = extractor.extract_patterns(sample_texts)
        domains = {f.domain for f in result.metaphor_families}
        assert "light" in domains

    def test_sample_texts_have_rhetorical_moves(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """The sample texts contain self-deprecation, paradox, and callback."""
        result = extractor.extract_patterns(sample_texts)
        assert result.rhetorical_moves.self_deprecation_count >= 1
        assert result.rhetorical_moves.paradox_count >= 1
        assert result.rhetorical_moves.callback_count >= 1

    def test_sample_texts_have_punctuation_variety(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """The sample texts use em-dashes, ellipsis, parentheticals, etc."""
        result = extractor.extract_patterns(sample_texts)
        assert result.punctuation.em_dash_frequency > 0.0
        assert result.punctuation.ellipsis_frequency > 0.0

    def test_sample_texts_sentence_count(
        self,
        extractor: VoicePatternExtractor,
        sample_texts: list[str],
    ) -> None:
        """The sample texts have a reasonable sentence count."""
        result = extractor.extract_patterns(sample_texts)
        assert result.sentence_metrics.total_sentences > 0
        assert result.sentence_metrics.average_length > 0.0
