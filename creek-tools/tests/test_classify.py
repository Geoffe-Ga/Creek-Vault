"""Tests for the creek.classify classification pipeline."""

import logging
from pathlib import Path

import pytest

from creek.classify import (
    LLMClassifier,
    ReviewQueueGenerator,
    RuleClassifier,
)
from creek.classify.llm import (
    CLASSIFICATION_PROMPT,
)
from creek.classify.llm import LLMClassifier as LLMClassifierDirect
from creek.classify.review import ReviewQueueGenerator as ReviewQueueGeneratorDirect
from creek.classify.rules import (
    CONFIDENCE_SIGNALS,
    FREQUENCY_SIGNALS,
    MODE_SIGNALS,
    VOICE_REGISTER_SIGNALS,
    WAVELENGTH_PHASE_SIGNALS,
)
from creek.classify.rules import RuleClassifier as RuleClassifierDirect
from creek.config import ClassificationConfig, LLMConfig
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
)

# ---- Helpers ----


def _make_fragment(
    title: str = "Test Fragment",
    platform: SourcePlatform = SourcePlatform.CLAUDE,
) -> Fragment:
    """Create a minimal Fragment for testing.

    Args:
        title: Title of the fragment.
        platform: Source platform for the fragment.

    Returns:
        A Fragment instance with the given title and platform.
    """
    return Fragment(
        title=title,
        source=FragmentSource(platform=platform),
    )


# ---- Module __init__ re-exports ----


class TestClassifyModuleExports:
    """Tests that creek.classify.__init__ re-exports key classes."""

    def test_rule_classifier_reexported(self) -> None:
        """RuleClassifier should be importable from creek.classify."""
        assert RuleClassifier is RuleClassifierDirect

    def test_llm_classifier_reexported(self) -> None:
        """LLMClassifier should be importable from creek.classify."""
        assert LLMClassifier is LLMClassifierDirect

    def test_review_queue_generator_reexported(self) -> None:
        """ReviewQueueGenerator should be importable from creek.classify."""
        assert ReviewQueueGenerator is ReviewQueueGeneratorDirect


# ---- Signal Dictionaries ----


class TestSignalDictionaries:
    """Tests for the keyword signal dictionaries in rules.py."""

    def test_frequency_signals_has_entries(self) -> None:
        """FREQUENCY_SIGNALS should have entries for frequencies."""
        assert len(FREQUENCY_SIGNALS) >= 2
        for key in FREQUENCY_SIGNALS:
            assert key in Frequency.__members__.values() or key in [
                f.value for f in Frequency if f != Frequency.UNCLASSIFIED
            ]

    def test_frequency_signals_values_are_keyword_lists(self) -> None:
        """Each frequency signal should be a non-empty list of strings."""
        for freq, keywords in FREQUENCY_SIGNALS.items():
            assert isinstance(keywords, list), f"{freq} not a list"
            assert len(keywords) >= 2, f"{freq} < 2 keywords"
            for kw in keywords:
                assert isinstance(kw, str), f"{kw!r} not str"

    def test_wavelength_phase_signals_has_entries(self) -> None:
        """WAVELENGTH_PHASE_SIGNALS should have entries with keywords."""
        assert len(WAVELENGTH_PHASE_SIGNALS) >= 2
        for _phase, keywords in WAVELENGTH_PHASE_SIGNALS.items():
            assert isinstance(keywords, list)
            assert len(keywords) >= 2

    def test_mode_signals_has_entries(self) -> None:
        """MODE_SIGNALS should have entries with keyword lists."""
        assert len(MODE_SIGNALS) >= 2
        for _mode, keywords in MODE_SIGNALS.items():
            assert isinstance(keywords, list)
            assert len(keywords) >= 2


# ---- RuleClassifier ----


class TestRuleClassifier:
    """Tests for the RuleClassifier class."""

    def test_instantiation(self) -> None:
        """RuleClassifier should instantiate without arguments."""
        classifier = RuleClassifier()
        assert isinstance(classifier, RuleClassifier)

    def test_classify_returns_fragment(self) -> None:
        """classify() should return a Fragment instance."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        result = classifier.classify(frag, content="some text")
        assert isinstance(result, Fragment)

    def test_classify_with_empty_content(self) -> None:
        """classify() with empty content returns fragment unchanged."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        result = classifier.classify(frag, content="")
        assert result.frequency.primary == Frequency.UNCLASSIFIED

    def test_classify_preserves_fragment_identity(self) -> None:
        """classify() should preserve the fragment's id and title."""
        classifier = RuleClassifier()
        frag = _make_fragment(title="My Title")
        result = classifier.classify(frag, content="some text")
        assert result.id == frag.id
        assert result.title == "My Title"

    def test_classify_matches_frequency_keywords(self) -> None:
        """classify() should set primary frequency on keyword match."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 1
        first_freq = next(iter(FREQUENCY_SIGNALS))
        keyword = FREQUENCY_SIGNALS[first_freq][0]
        frag = _make_fragment()
        content = f"Talking about {keyword} today"
        result = classifier.classify(frag, content=content)
        assert result.frequency.primary != Frequency.UNCLASSIFIED

    def test_classify_matches_phase_keywords(self) -> None:
        """classify() should set wavelength phase on keyword match."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 1
        first_phase = next(iter(WAVELENGTH_PHASE_SIGNALS))
        keyword = WAVELENGTH_PHASE_SIGNALS[first_phase][0]
        frag = _make_fragment()
        content = f"Feeling {keyword} in my life"
        result = classifier.classify(frag, content=content)
        assert result.wavelength.phase != Phase.UNCLASSIFIED

    def test_classify_matches_mode_keywords(self) -> None:
        """classify() should set wavelength mode on keyword match."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 1
        first_mode = next(iter(MODE_SIGNALS))
        keyword = MODE_SIGNALS[first_mode][0]
        frag = _make_fragment()
        content = f"I need to {keyword} this idea"
        result = classifier.classify(frag, content=content)
        assert result.wavelength.mode != Mode.UNCLASSIFIED

    def test_classify_no_match_leaves_unclassified(self) -> None:
        """classify() with no matches leaves fields unclassified."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        content = "xyzzy plugh nothing matches here"
        result = classifier.classify(frag, content=content)
        assert result.frequency.primary == Frequency.UNCLASSIFIED
        assert result.wavelength.phase == Phase.UNCLASSIFIED
        assert result.wavelength.mode == Mode.UNCLASSIFIED

    def test_classify_case_insensitive(self) -> None:
        """classify() keyword matching should be case-insensitive."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 1
        first_freq = next(iter(FREQUENCY_SIGNALS))
        keyword = FREQUENCY_SIGNALS[first_freq][0]
        frag = _make_fragment()
        result = classifier.classify(frag, content=keyword.upper())
        assert result.frequency.primary != Frequency.UNCLASSIFIED

    def test_classify_default_content_parameter(self) -> None:
        """classify() should accept content as empty string default."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        result = classifier.classify(frag)
        assert isinstance(result, Fragment)

    def test_classify_does_not_mutate_original(self) -> None:
        """classify() should not mutate the original fragment."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        original_freq = frag.frequency.primary
        first_freq = next(iter(FREQUENCY_SIGNALS))
        keyword = FREQUENCY_SIGNALS[first_freq][0]
        _ = classifier.classify(frag, content=keyword)
        assert frag.frequency.primary == original_freq


# ---- LLMClassifier ----


class TestLLMClassifier:
    """Tests for the LLMClassifier stub class."""

    def test_instantiation_with_config(self) -> None:
        """LLMClassifier should accept an LLMConfig on init."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier.config is config

    def test_classify_returns_fragment_unchanged(self) -> None:
        """classify() stub should return the fragment unchanged."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        result = classifier.classify(frag)
        assert result.id == frag.id
        assert result.title == frag.title
        assert result.frequency.primary == frag.frequency.primary

    def test_classify_logs_message(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """classify() should log a message about being a stub."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment(title="Log Test")
        with caplog.at_level(logging.INFO):
            classifier.classify(frag)
        assert any(
            "stub" in record.message.lower() or "llm" in record.message.lower()
            for record in caplog.records
        )

    def test_classify_batch_returns_unchanged(self) -> None:
        """classify_batch() should return all fragments unchanged."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frags = [_make_fragment(title=f"Frag {i}") for i in range(3)]
        results = classifier.classify_batch(frags)
        assert len(results) == 3
        for original, result in zip(frags, results, strict=True):
            assert result.id == original.id
            assert result.title == original.title

    def test_classify_batch_empty_list(self) -> None:
        """classify_batch() with empty list returns empty list."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        results = classifier.classify_batch([])
        assert results == []

    def test_classify_batch_logs_message(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """classify_batch() should log about stub processing."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frags = [_make_fragment(title=f"Batch {i}") for i in range(2)]
        with caplog.at_level(logging.INFO):
            classifier.classify_batch(frags)
        assert len(caplog.records) > 0

    def test_classification_prompt_is_nonempty_string(self) -> None:
        """CLASSIFICATION_PROMPT should be a non-empty string."""
        assert isinstance(CLASSIFICATION_PROMPT, str)
        assert len(CLASSIFICATION_PROMPT) > 0

    def test_classification_prompt_contains_key_terms(self) -> None:
        """CLASSIFICATION_PROMPT should reference key concepts."""
        prompt_lower = CLASSIFICATION_PROMPT.lower()
        assert "frequency" in prompt_lower
        assert "fragment" in prompt_lower or "content" in prompt_lower

    def test_config_stored_on_instance(self) -> None:
        """LLMClassifier should store config as an attribute."""
        config = LLMConfig(provider="anthropic", model="claude-3")
        classifier = LLMClassifier(config=config)
        assert classifier.config.provider == "anthropic"
        assert classifier.config.model == "claude-3"


# ---- ReviewQueueGenerator ----


class TestReviewQueueGenerator:
    """Tests for the ReviewQueueGenerator class."""

    def test_instantiation(self) -> None:
        """ReviewQueueGenerator should instantiate without arguments."""
        generator = ReviewQueueGenerator()
        assert isinstance(generator, ReviewQueueGenerator)

    def test_instantiation_with_config(self) -> None:
        """ReviewQueueGenerator should accept ClassificationConfig."""
        config = ClassificationConfig()
        generator = ReviewQueueGenerator(config=config)
        assert generator.config is config

    def test_needs_review_unclassified_fragment(self) -> None:
        """needs_review() returns True for unclassified fragments."""
        generator = ReviewQueueGenerator()
        frag = _make_fragment()
        assert generator.needs_review(frag) is True

    def test_needs_review_classified_auto_source(self) -> None:
        """needs_review() returns False for classified auto source."""
        config = ClassificationConfig(
            auto_classify_sources=["claude"],
        )
        generator = ReviewQueueGenerator(config=config)
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        frag = frag.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F3,
                ),
                "voice": VoiceClassification(confidence="settled"),
            },
        )
        assert generator.needs_review(frag) is False

    def test_needs_review_human_review_source(self) -> None:
        """needs_review() returns True for human_review_sources."""
        config = ClassificationConfig(
            human_review_sources=["journal"],
        )
        generator = ReviewQueueGenerator(config=config)
        frag = _make_fragment(platform=SourcePlatform.JOURNAL)
        frag = frag.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F5,
                ),
                "voice": VoiceClassification(confidence="settled"),
            },
        )
        assert generator.needs_review(frag) is True

    def test_needs_review_low_confidence(self) -> None:
        """needs_review() returns True when confidence is low."""
        generator = ReviewQueueGenerator()
        frag = _make_fragment()
        frag = frag.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F1,
                ),
                "voice": VoiceClassification(confidence="musing"),
            },
        )
        assert generator.needs_review(frag) is True

    def test_needs_review_no_confidence(self) -> None:
        """needs_review() returns True when confidence is None."""
        generator = ReviewQueueGenerator()
        frag = _make_fragment()
        frag = frag.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F1,
                ),
                "voice": VoiceClassification(confidence=None),
            },
        )
        assert generator.needs_review(frag) is True

    def test_generate_queue_creates_file(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() should create a markdown file."""
        generator = ReviewQueueGenerator()
        frags = [_make_fragment(title="Needs Review")]
        result = generator.generate_queue(frags, tmp_path)
        assert result.exists()
        assert result.suffix == ".md"

    def test_generate_queue_contains_checkboxes(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() output should contain checkboxes."""
        generator = ReviewQueueGenerator()
        frags = [
            _make_fragment(title="Fragment Alpha"),
            _make_fragment(title="Fragment Beta"),
        ]
        result = generator.generate_queue(frags, tmp_path)
        content = result.read_text()
        assert "- [ ]" in content
        assert "Fragment Alpha" in content
        assert "Fragment Beta" in content

    def test_generate_queue_empty_list(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() with no fragments creates file anyway."""
        generator = ReviewQueueGenerator()
        result = generator.generate_queue([], tmp_path)
        assert result.exists()
        content = result.read_text()
        assert "- [ ]" not in content

    def test_generate_queue_returns_path_in_vault(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() path should be inside vault directory."""
        generator = ReviewQueueGenerator()
        frags = [_make_fragment()]
        result = generator.generate_queue(frags, tmp_path)
        assert str(result).startswith(str(tmp_path))

    def test_generate_queue_includes_fragment_ids(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() should include fragment IDs."""
        generator = ReviewQueueGenerator()
        frag = _make_fragment(title="Traceable")
        frags = [frag]
        result = generator.generate_queue(frags, tmp_path)
        content = result.read_text()
        assert frag.id in content

    def test_generate_queue_includes_frequency_info(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() should include frequency info."""
        generator = ReviewQueueGenerator()
        frag = _make_fragment(title="Classified Fragment")
        frag = frag.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F7,
                ),
            },
        )
        frags = [frag]
        result = generator.generate_queue(frags, tmp_path)
        content = result.read_text()
        assert "F7" in content

    def test_generate_queue_file_has_header(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() file should have a markdown header."""
        generator = ReviewQueueGenerator()
        frags = [_make_fragment()]
        result = generator.generate_queue(frags, tmp_path)
        content = result.read_text()
        assert content.startswith("#")

    def test_generate_queue_filters_needing_review(
        self,
        tmp_path: Path,
    ) -> None:
        """generate_queue() should only include review-needing items."""
        config = ClassificationConfig(
            auto_classify_sources=["claude"],
            human_review_sources=["journal"],
        )
        generator = ReviewQueueGenerator(config=config)

        # Fragment that needs review (unclassified)
        frag_needs = _make_fragment(title="Needs Review")

        # Fragment that does NOT need review
        frag_ok = _make_fragment(
            title="Already Classified",
            platform=SourcePlatform.CLAUDE,
        )
        frag_ok = frag_ok.model_copy(
            update={
                "frequency": FrequencyClassification(
                    primary=Frequency.F3,
                ),
                "voice": VoiceClassification(confidence="settled"),
            },
        )

        frags = [frag_needs, frag_ok]
        result = generator.generate_queue(frags, tmp_path)
        content = result.read_text()
        assert "Needs Review" in content
        assert "Already Classified" not in content


# ---- Expanded Signal Dictionaries (Issue #23) ----


class TestExpandedSignalDictionaries:
    """Tests that all frequencies, phases, and modes have keyword lists."""

    def test_all_ten_frequencies_present(self) -> None:
        """FREQUENCY_SIGNALS should cover all 10 frequencies."""
        expected = {
            Frequency.F1,
            Frequency.F2,
            Frequency.F3,
            Frequency.F4,
            Frequency.F5,
            Frequency.F6,
            Frequency.F7,
            Frequency.F8,
            Frequency.F9,
            Frequency.F10,
        }
        assert set(FREQUENCY_SIGNALS.keys()) == expected

    def test_each_frequency_has_enough_keywords(self) -> None:
        """Each frequency should have at least 9 keywords."""
        for freq, keywords in FREQUENCY_SIGNALS.items():
            assert len(keywords) >= 9, f"{freq} has only {len(keywords)}"

    def test_all_six_phases_present(self) -> None:
        """WAVELENGTH_PHASE_SIGNALS should cover all 6 phases."""
        expected = {
            Phase.RISING,
            Phase.PEAKING,
            Phase.WITHDRAWAL,
            Phase.DIMINISHING,
            Phase.BOTTOMING_OUT,
            Phase.RESTORATION,
        }
        assert set(WAVELENGTH_PHASE_SIGNALS.keys()) == expected

    def test_each_phase_has_enough_keywords(self) -> None:
        """Each phase should have at least 7 keywords."""
        for phase, keywords in WAVELENGTH_PHASE_SIGNALS.items():
            assert len(keywords) >= 7, f"{phase} has only {len(keywords)}"

    def test_all_five_modes_present(self) -> None:
        """MODE_SIGNALS should cover all 5 modes."""
        expected = {
            Mode.INHABIT,
            Mode.EXPRESS,
            Mode.COLLABORATE,
            Mode.INTEGRATE,
            Mode.ABSORB,
        }
        assert set(MODE_SIGNALS.keys()) == expected

    def test_each_mode_has_enough_keywords(self) -> None:
        """Each mode should have at least 8 keywords."""
        for mode, keywords in MODE_SIGNALS.items():
            assert len(keywords) >= 8, f"{mode} has only {len(keywords)}"


# ---- Voice Register & Confidence Signals ----


class TestVoiceRegisterSignals:
    """Tests for voice register and confidence signal dictionaries."""

    def test_voice_register_signals_exist(self) -> None:
        """VOICE_REGISTER_SIGNALS should be a non-empty dict."""
        assert isinstance(VOICE_REGISTER_SIGNALS, dict)
        assert len(VOICE_REGISTER_SIGNALS) >= 5

    def test_voice_register_keys_are_valid(self) -> None:
        """All keys should be valid VoiceRegister values."""
        for key in VOICE_REGISTER_SIGNALS:
            assert isinstance(key, VoiceRegister)

    def test_confidence_signals_exist(self) -> None:
        """CONFIDENCE_SIGNALS should be a non-empty dict."""
        assert isinstance(CONFIDENCE_SIGNALS, dict)
        assert len(CONFIDENCE_SIGNALS) >= 4

    def test_confidence_keys_are_valid(self) -> None:
        """All keys should be valid Confidence values."""
        for key in CONFIDENCE_SIGNALS:
            assert isinstance(key, Confidence)


# ---- Scoring Classifier ----


class TestScoringClassifier:
    """Tests for the scoring-based classify mechanism."""

    def test_title_match_weighted_higher(self) -> None:
        """Title keyword matches should score higher than body."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 2
        frag = _make_fragment(title="survival safety threat")
        result = classifier.classify(frag, content="unrelated body text")
        assert result.frequency.primary == Frequency.F1

    def test_multiple_keywords_increase_score(self) -> None:
        """More keyword matches should produce a classification."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        content = (
            "survival safety security threat danger\n\n"
            "shelter hunger thirst instinct primal"
        )
        result = classifier.classify(frag, content=content)
        assert result.frequency.primary == Frequency.F1

    def test_below_threshold_leaves_unclassified(self) -> None:
        """A single body keyword should not trigger classification."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 5
        frag = _make_fragment()
        result = classifier.classify(frag, content="survival")
        assert result.frequency.primary == Frequency.UNCLASSIFIED

    def test_configurable_thresholds(self) -> None:
        """Thresholds should be configurable per instance."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 1
        frag = _make_fragment()
        result = classifier.classify(frag, content="survival")
        assert result.frequency.primary == Frequency.F1


# ---- Secondary Frequencies ----


class TestSecondaryFrequencies:
    """Tests for multi-frequency tagging."""

    def test_secondary_frequencies_populated(self) -> None:
        """Fragments with mixed signals should get secondaries."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 2
        classifier.SECONDARY_THRESHOLD = 2
        content = "survival safety security\n\npower dominance control"
        frag = _make_fragment()
        result = classifier.classify(frag, content=content)
        assert result.frequency.primary != Frequency.UNCLASSIFIED
        assert len(result.frequency.secondary) >= 1

    def test_no_secondaries_when_only_primary(self) -> None:
        """No secondaries when only one frequency scores."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 2
        classifier.SECONDARY_THRESHOLD = 2
        frag = _make_fragment()
        content = "survival safety security threat"
        result = classifier.classify(frag, content=content)
        assert result.frequency.secondary == []


# ---- Confidence Score ----


class TestConfidenceScore:
    """Tests for the confidence_score() method."""

    def test_confidence_score_returns_float(self) -> None:
        """confidence_score() should return a float."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        score = classifier.confidence_score(frag, content="some text")
        assert isinstance(score, float)

    def test_confidence_score_range(self) -> None:
        """confidence_score() should be between 0.0 and 1.0."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        score = classifier.confidence_score(frag, content="random text")
        assert 0.0 <= score <= 1.0

    def test_high_confidence_for_clear_signals(self) -> None:
        """Strong signals across dimensions should yield high confidence."""
        classifier = RuleClassifier()
        content = (
            "survival safety security threat danger shelter\n\n"
            "emerging building growing momentum ascending\n\n"
            "dwelling immersed inhabiting living in being with"
        )
        frag = _make_fragment(title="survival and safety")
        score = classifier.confidence_score(frag, content=content)
        assert score > 0.3

    def test_low_confidence_for_no_signals(self) -> None:
        """No matching signals should yield low confidence."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        score = classifier.confidence_score(frag, content="xyzzy plugh")
        assert score < 0.1


# ---- Word Boundary Matching ----


class TestWordBoundaryMatching:
    """Tests that keyword matching uses word boundaries."""

    def test_power_matches_standalone(self) -> None:
        """'power' should match as a standalone word."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 1
        frag = _make_fragment()
        result = classifier.classify(frag, content="raw power")
        assert result.frequency.primary == Frequency.F3

    def test_empower_does_not_match_power(self) -> None:
        """'empower' should NOT match F3 keyword 'power'."""
        classifier = RuleClassifier()
        classifier.PRIMARY_THRESHOLD = 1
        frag = _make_fragment(title="unrelated title")
        result = classifier.classify(frag, content="empower yourself")
        assert result.frequency.primary != Frequency.F3


# ---- Voice Register Classification ----


class TestVoiceRegisterClassification:
    """Tests for voice register and confidence classification."""

    def test_voice_register_detected(self) -> None:
        """Voice register should be set from keyword signals."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 2
        content = (
            "I confess I must admit this is deeply personal\n\n"
            "I reveal my vulnerable honest truth"
        )
        frag = _make_fragment()
        result = classifier.classify(frag, content=content)
        assert result.voice.voice_register is not None

    def test_confidence_detected(self) -> None:
        """Confidence level should be set from keyword signals."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 2
        content = (
            "maybe perhaps I'm wondering what if\n\ncould be not sure possibly might"
        )
        frag = _make_fragment()
        result = classifier.classify(frag, content=content)
        assert result.voice.confidence is not None

    def test_no_voice_on_ambiguous_content(self) -> None:
        """Voice register should be None for ambiguous content."""
        classifier = RuleClassifier()
        frag = _make_fragment()
        result = classifier.classify(frag, content="xyzzy plugh")
        if result.voice is not None:
            assert result.voice.voice_register is None


class TestScoreSignalsGeneralized:
    """Tests for generalized _score_signals accepting any enum key type."""

    def test_score_signals_with_voice_register(self) -> None:
        """_score_signals should work with VoiceRegister signal dicts."""
        classifier = RuleClassifier()
        scores = classifier._score_signals(
            "confess admit reveal",
            "",
            "",
            VOICE_REGISTER_SIGNALS,
        )
        assert isinstance(scores, dict)
        assert VoiceRegister.CONFESSIONAL in scores
        assert scores[VoiceRegister.CONFESSIONAL] > 0

    def test_score_signals_with_confidence(self) -> None:
        """_score_signals should work with Confidence signal dicts."""
        classifier = RuleClassifier()
        scores = classifier._score_signals(
            "maybe perhaps wondering",
            "",
            "",
            CONFIDENCE_SIGNALS,
        )
        assert isinstance(scores, dict)
        assert Confidence.MUSING in scores
        assert scores[Confidence.MUSING] > 0

    def test_confidence_score_weights_are_class_constants(self) -> None:
        """Confidence score weights should be configurable class constants."""
        classifier = RuleClassifier()
        assert hasattr(classifier, "CONFIDENCE_MATCH_WEIGHT")
        assert hasattr(classifier, "CONFIDENCE_DIMENSION_WEIGHT")
        assert hasattr(classifier, "CONFIDENCE_GAP_WEIGHT")
        total = (
            classifier.CONFIDENCE_MATCH_WEIGHT
            + classifier.CONFIDENCE_DIMENSION_WEIGHT
            + classifier.CONFIDENCE_GAP_WEIGHT
        )
        assert abs(total - 1.0) < 1e-9

    def test_score_signals_returns_enum_keys_for_frequency(self) -> None:
        """_score_signals with FREQUENCY_SIGNALS returns Frequency-keyed dict."""
        classifier = RuleClassifier()
        scores = classifier._score_signals(
            "survival",
            "",
            "",
            FREQUENCY_SIGNALS,
        )
        assert all(isinstance(k, Frequency) for k in scores)

    def test_score_signals_returns_enum_keys_for_phase(self) -> None:
        """_score_signals with WAVELENGTH_PHASE_SIGNALS returns Phase-keyed dict."""
        classifier = RuleClassifier()
        scores = classifier._score_signals(
            "emerging",
            "",
            "",
            WAVELENGTH_PHASE_SIGNALS,
        )
        assert all(isinstance(k, Phase) for k in scores)

    def test_score_signals_returns_enum_keys_for_mode(self) -> None:
        """_score_signals with MODE_SIGNALS returns Mode-keyed dict."""
        classifier = RuleClassifier()
        scores = classifier._score_signals(
            "dwelling",
            "",
            "",
            MODE_SIGNALS,
        )
        assert all(isinstance(k, Mode) for k in scores)

    def test_score_signals_never_empty_for_nonempty_signals(self) -> None:
        """_score_signals always returns non-empty dict for non-empty signals."""
        classifier = RuleClassifier()
        for signals in (
            FREQUENCY_SIGNALS,
            WAVELENGTH_PHASE_SIGNALS,
            MODE_SIGNALS,
            VOICE_REGISTER_SIGNALS,
            CONFIDENCE_SIGNALS,
        ):
            scores = classifier._score_signals("", "", "", signals)
            assert len(scores) > 0


class TestMatchVoiceRegisterNoDeadCode:
    """Tests confirming _match_voice_register works without empty-dict guard."""

    def test_match_voice_register_no_match_returns_none(self) -> None:
        """_match_voice_register returns None when no keywords match."""
        classifier = RuleClassifier()
        result = classifier._match_voice_register("xyzzy", "", "")
        assert result is None

    def test_match_voice_register_with_match(self) -> None:
        """_match_voice_register returns a VoiceRegister on match."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 1
        result = classifier._match_voice_register(
            "confess admit",
            "",
            "",
        )
        assert isinstance(result, VoiceRegister)


class TestMatchConfidenceNoDeadCode:
    """Tests confirming _match_confidence works without empty-dict guard."""

    def test_match_confidence_no_match_returns_none(self) -> None:
        """_match_confidence returns None when no keywords match."""
        classifier = RuleClassifier()
        result = classifier._match_confidence("xyzzy", "", "")
        assert result is None

    def test_match_confidence_with_match(self) -> None:
        """_match_confidence returns a Confidence value on match."""
        classifier = RuleClassifier()
        classifier.SECONDARY_THRESHOLD = 1
        result = classifier._match_confidence(
            "maybe perhaps",
            "",
            "",
        )
        assert isinstance(result, Confidence)
