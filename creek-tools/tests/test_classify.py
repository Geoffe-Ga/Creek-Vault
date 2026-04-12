"""Tests for the creek.classify classification pipeline."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from creek.classify import (
    LLMClassifier,
    ReviewQueueGenerator,
    RuleClassifier,
)
from creek.classify.llm import (
    ANTHROPIC_CLOUD_WARNING,
    CLASSIFICATION_PROMPT,
    AnthropicProvider,
    BatchStats,
    _parse_dosage,
    _parse_enum,
    _parse_optional_enum,
    _strip_code_fences,
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
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
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


# ---- Valid YAML response for mocking ----

_VALID_YAML_RESPONSE: str = """\
frequency:
  primary: F3
  secondary: [F5, F7]
wavelength:
  phase: rising
  mode: express
  orientation: do
  dosage: medicine
voice:
  voice_register: analytical
  confidence: forming
"""

_OLLAMA_JSON_RESPONSE: dict[str, object] = {
    "response": _VALID_YAML_RESPONSE,
    "done": True,
}


def _make_classifier_available(
    classifier: LLMClassifier,
) -> LLMClassifier:
    """Force the classifier to think Ollama is available.

    Args:
        classifier: The classifier to modify.

    Returns:
        The same classifier with availability forced.
    """
    classifier._available = True
    return classifier


def _make_classifier_unavailable(
    classifier: LLMClassifier,
) -> LLMClassifier:
    """Force the classifier to think Ollama is unavailable.

    Args:
        classifier: The classifier to modify.

    Returns:
        The same classifier with availability forced off.
    """
    classifier._available = False
    return classifier


# ---- LLMClassifier Initialization ----


class TestLLMClassifierInit:
    """Tests for LLMClassifier initialization."""

    def test_stores_config(self) -> None:
        """LLMClassifier should store the provided config."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier.config is config

    def test_config_values_accessible(self) -> None:
        """Config values should be accessible on the instance."""
        config = LLMConfig(provider="anthropic", model="claude-3")
        classifier = LLMClassifier(config=config)
        assert classifier.config.provider == "anthropic"
        assert classifier.config.model == "claude-3"

    def test_availability_not_checked_on_init(self) -> None:
        """Availability should not be checked during __init__."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier._available is None


# ---- Availability ----


class TestLLMClassifierAvailability:
    """Tests for Ollama availability checking."""

    @patch("creek.classify.llm.httpx.Client")
    def test_available_when_ollama_responds_200(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """available should be True when Ollama returns 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_ctx = MagicMock()
        mock_ctx.get.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(
            return_value=mock_ctx,
        )
        mock_client_cls.return_value.__exit__ = MagicMock(
            return_value=False,
        )

        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier.available is True

    @patch("creek.classify.llm.httpx.Client")
    def test_unavailable_on_connection_error(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """available should be False when connection fails."""
        mock_ctx = MagicMock()
        mock_ctx.get.side_effect = httpx.ConnectError("refused")
        mock_client_cls.return_value.__enter__ = MagicMock(
            return_value=mock_ctx,
        )
        mock_client_cls.return_value.__exit__ = MagicMock(
            return_value=False,
        )

        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier.available is False

    @patch("creek.classify.llm.httpx.Client")
    def test_unavailable_on_non_200(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """available should be False on non-200 response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_ctx = MagicMock()
        mock_ctx.get.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(
            return_value=mock_ctx,
        )
        mock_client_cls.return_value.__exit__ = MagicMock(
            return_value=False,
        )

        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        assert classifier.available is False

    def test_availability_cached(self) -> None:
        """Availability result should be cached after first check."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        classifier._available = True
        assert classifier.available is True

    @patch("creek.classify.llm.httpx.Client")
    def test_logs_warning_when_unavailable(
        self,
        mock_client_cls: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A warning should be logged when Ollama is unavailable."""
        mock_ctx = MagicMock()
        mock_ctx.get.side_effect = httpx.ConnectError("refused")
        mock_client_cls.return_value.__enter__ = MagicMock(
            return_value=mock_ctx,
        )
        mock_client_cls.return_value.__exit__ = MagicMock(
            return_value=False,
        )

        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        with caplog.at_level(logging.WARNING):
            _ = classifier.available
        assert any("not available" in r.message.lower() for r in caplog.records)


# ---- Prompt Building ----


class TestBuildPrompt:
    """Tests for _build_prompt."""

    def test_prompt_includes_title(self) -> None:
        """Prompt should include the fragment title."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment(title="My Title")
        prompt = classifier._build_prompt(frag)
        assert "My Title" in prompt

    def test_prompt_includes_content(self) -> None:
        """Prompt should include provided content."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        prompt = classifier._build_prompt(frag, content="Hello world")
        assert "Hello world" in prompt

    def test_prompt_placeholder_when_no_content(self) -> None:
        """Prompt should show placeholder when no content given."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        prompt = classifier._build_prompt(frag)
        assert "no content provided" in prompt


# ---- CLASSIFICATION_PROMPT ----


class TestClassificationPrompt:
    """Tests for the CLASSIFICATION_PROMPT constant."""

    def test_is_nonempty_string(self) -> None:
        """CLASSIFICATION_PROMPT should be a non-empty string."""
        assert isinstance(CLASSIFICATION_PROMPT, str)
        assert len(CLASSIFICATION_PROMPT) > 0

    def test_contains_key_terms(self) -> None:
        """CLASSIFICATION_PROMPT should reference key concepts."""
        prompt_lower = CLASSIFICATION_PROMPT.lower()
        assert "frequency" in prompt_lower
        assert "fragment" in prompt_lower or "content" in prompt_lower

    def test_contains_yaml_format(self) -> None:
        """CLASSIFICATION_PROMPT should request YAML output."""
        assert "YAML" in CLASSIFICATION_PROMPT

    def test_has_format_placeholders(self) -> None:
        """CLASSIFICATION_PROMPT should have title and content placeholders."""
        assert "{title}" in CLASSIFICATION_PROMPT
        assert "{content}" in CLASSIFICATION_PROMPT


# ---- Validate Response ----


class TestValidateResponse:
    """Tests for validate_response."""

    def test_valid_yaml_parsed(self) -> None:
        """Valid YAML dict should be parsed successfully."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        result = classifier.validate_response(_VALID_YAML_RESPONSE)
        assert isinstance(result, dict)
        assert "frequency" in result

    def test_invalid_yaml_raises(self) -> None:
        """Invalid YAML should raise ValueError."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        with pytest.raises((ValueError, yaml.YAMLError)):
            classifier.validate_response("{{invalid yaml::")

    def test_non_dict_yaml_raises(self) -> None:
        """YAML that parses to a non-dict should raise ValueError."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        with pytest.raises(ValueError, match="Expected YAML dict"):
            classifier.validate_response("- item1\n- item2")

    def test_strips_code_fences(self) -> None:
        """Should handle YAML wrapped in markdown code fences."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        fenced = f"```yaml\n{_VALID_YAML_RESPONSE}```"
        result = classifier.validate_response(fenced)
        assert "frequency" in result

    def test_empty_string_raises(self) -> None:
        """Empty string should raise ValueError."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        with pytest.raises(ValueError, match="Expected YAML dict"):
            classifier.validate_response("")


# ---- Apply Classification ----


class TestApplyClassification:
    """Tests for _apply_classification."""

    def test_applies_frequency(self) -> None:
        """Frequency fields should be applied to fragment."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        data: dict[str, object] = {
            "frequency": {"primary": "F3", "secondary": ["F5"]},
        }
        result = classifier._apply_classification(frag, data)
        assert result.frequency.primary == Frequency.F3
        assert Frequency.F5 in result.frequency.secondary

    def test_applies_wavelength(self) -> None:
        """Wavelength fields should be applied to fragment."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        data: dict[str, object] = {
            "wavelength": {
                "phase": "rising",
                "mode": "express",
                "orientation": "do",
                "dosage": "medicine",
            },
        }
        result = classifier._apply_classification(frag, data)
        assert result.wavelength.phase == Phase.RISING
        assert result.wavelength.mode == Mode.EXPRESS
        assert result.wavelength.orientation == Orientation.DO
        assert result.wavelength.dosage == Dosage.MEDICINE

    def test_applies_voice(self) -> None:
        """Voice fields should be applied to fragment."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        data: dict[str, object] = {
            "voice": {
                "voice_register": "analytical",
                "confidence": "forming",
            },
        }
        result = classifier._apply_classification(frag, data)
        assert result.voice.voice_register == VoiceRegister.ANALYTICAL
        assert result.voice.confidence == Confidence.FORMING

    def test_preserves_fragment_when_no_data(self) -> None:
        """Fragment should be unchanged when data has no sections."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        result = classifier._apply_classification(frag, {})
        assert result is frag

    def test_handles_partial_data(self) -> None:
        """Only present sections should update the fragment."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        data: dict[str, object] = {
            "frequency": {"primary": "F7"},
        }
        result = classifier._apply_classification(frag, data)
        assert result.frequency.primary == Frequency.F7
        assert result.wavelength.phase == Phase.UNCLASSIFIED

    def test_ambiguous_dosage_markers(self) -> None:
        """Ambiguous dosage markers should map to Dosage.AMBIGUOUS."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        for marker in ("ambiguous", "unclear", "mixed", "both"):
            data: dict[str, object] = {
                "wavelength": {"dosage": marker},
            }
            result = classifier._apply_classification(frag, data)
            assert result.wavelength.dosage == Dosage.AMBIGUOUS

    def test_does_not_mutate_original(self) -> None:
        """_apply_classification should not mutate the original fragment."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        frag = _make_fragment()
        original_freq = frag.frequency.primary
        data: dict[str, object] = {
            "frequency": {"primary": "F3"},
        }
        classifier._apply_classification(frag, data)
        assert frag.frequency.primary == original_freq


# ---- Classify (single) ----


class TestClassify:
    """Tests for the classify method with mocked Ollama."""

    @patch.object(LLMClassifier, "_call_ollama")
    def test_classifies_fragment(
        self,
        mock_call: MagicMock,
    ) -> None:
        """classify should update fragment from LLM response."""
        mock_call.return_value = _VALID_YAML_RESPONSE
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        frag = _make_fragment(title="Test")
        result = classifier.classify(frag)
        assert result.frequency.primary == Frequency.F3

    def test_returns_unchanged_when_unavailable(self) -> None:
        """classify should return fragment unchanged when unavailable."""
        config = LLMConfig()
        classifier = _make_classifier_unavailable(
            LLMClassifier(config=config),
        )
        frag = _make_fragment()
        result = classifier.classify(frag)
        assert result.id == frag.id
        assert result.frequency.primary == Frequency.UNCLASSIFIED

    @patch.object(LLMClassifier, "_call_ollama")
    @patch("creek.classify.llm.time.sleep")
    def test_retries_on_malformed_response(
        self,
        mock_sleep: MagicMock,
        mock_call: MagicMock,
    ) -> None:
        """classify should retry when response is malformed."""
        mock_call.side_effect = [
            "not valid yaml {{",
            _VALID_YAML_RESPONSE,
        ]
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        result = classifier.classify(_make_fragment())
        assert result.frequency.primary == Frequency.F3
        assert mock_call.call_count == 2

    @patch.object(LLMClassifier, "_call_ollama")
    @patch("creek.classify.llm.time.sleep")
    def test_returns_unchanged_after_all_retries(
        self,
        mock_sleep: MagicMock,
        mock_call: MagicMock,
    ) -> None:
        """classify should return unchanged after exhausting retries."""
        mock_call.side_effect = httpx.ConnectError("fail")
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        classifier.MAX_RETRIES = 2
        frag = _make_fragment()
        result = classifier.classify(frag)
        assert result.frequency.primary == Frequency.UNCLASSIFIED
        assert mock_call.call_count == 2

    @patch.object(LLMClassifier, "_call_ollama")
    def test_logs_warning_when_unavailable(
        self,
        mock_call: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """classify should log warning when Ollama is unavailable."""
        config = LLMConfig()
        classifier = _make_classifier_unavailable(
            LLMClassifier(config=config),
        )
        with caplog.at_level(logging.WARNING):
            classifier.classify(_make_fragment(title="Warn Test"))
        assert any("unavailable" in r.message.lower() for r in caplog.records)
        mock_call.assert_not_called()

    @patch.object(LLMClassifier, "_call_ollama")
    def test_classify_preserves_id_and_title(
        self,
        mock_call: MagicMock,
    ) -> None:
        """classify should preserve the fragment's id and title."""
        mock_call.return_value = _VALID_YAML_RESPONSE
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        frag = _make_fragment(title="Keep Me")
        result = classifier.classify(frag)
        assert result.id == frag.id
        assert result.title == "Keep Me"


# ---- Classify Batch ----


class TestClassifyBatch:
    """Tests for classify_batch."""

    @patch.object(LLMClassifier, "_call_ollama")
    @patch("creek.classify.llm.time.sleep")
    def test_batch_returns_all_fragments(
        self,
        mock_sleep: MagicMock,
        mock_call: MagicMock,
    ) -> None:
        """classify_batch should return same number of fragments."""
        mock_call.return_value = _VALID_YAML_RESPONSE
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        frags = [_make_fragment(title=f"F{i}") for i in range(3)]
        results = classifier.classify_batch(frags, progress=False)
        assert len(results) == 3

    def test_batch_empty_list(self) -> None:
        """classify_batch with empty list returns empty list."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        results = classifier.classify_batch([])
        assert results == []

    def test_batch_returns_unchanged_when_unavailable(self) -> None:
        """classify_batch returns unchanged when Ollama unavailable."""
        config = LLMConfig()
        classifier = _make_classifier_unavailable(
            LLMClassifier(config=config),
        )
        frags = [_make_fragment(title=f"F{i}") for i in range(3)]
        results = classifier.classify_batch(frags, progress=False)
        assert len(results) == 3
        for orig, res in zip(frags, results, strict=True):
            assert res.id == orig.id

    @patch.object(LLMClassifier, "_call_ollama")
    @patch("creek.classify.llm.time.sleep")
    def test_batch_classifies_fragments(
        self,
        mock_sleep: MagicMock,
        mock_call: MagicMock,
    ) -> None:
        """classify_batch should classify all fragments."""
        mock_call.return_value = _VALID_YAML_RESPONSE
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        frags = [_make_fragment(title=f"F{i}") for i in range(2)]
        results = classifier.classify_batch(frags, progress=False)
        for res in results:
            assert res.frequency.primary == Frequency.F3

    @patch.object(LLMClassifier, "_call_ollama")
    @patch("creek.classify.llm.time.sleep")
    def test_batch_logs_stats(
        self,
        mock_sleep: MagicMock,
        mock_call: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """classify_batch should log aggregated stats."""
        mock_call.return_value = _VALID_YAML_RESPONSE
        config = LLMConfig()
        classifier = _make_classifier_available(
            LLMClassifier(config=config),
        )
        frags = [_make_fragment(title=f"F{i}") for i in range(2)]
        with caplog.at_level(logging.INFO):
            classifier.classify_batch(frags, progress=False)
        assert any("batch complete" in r.message.lower() for r in caplog.records)


# ---- Parse Helpers ----


class TestParseEnum:
    """Tests for _parse_enum helper."""

    def test_parses_valid_frequency(self) -> None:
        """Should parse a valid frequency string."""
        result = _parse_enum("F3", Frequency, Frequency.UNCLASSIFIED)
        assert result == Frequency.F3

    def test_returns_default_for_unknown(self) -> None:
        """Should return default for unknown value."""
        result = _parse_enum("xyz", Frequency, Frequency.UNCLASSIFIED)
        assert result == Frequency.UNCLASSIFIED

    def test_returns_default_for_none(self) -> None:
        """Should return default for None value."""
        result = _parse_enum(None, Frequency, Frequency.UNCLASSIFIED)
        assert result == Frequency.UNCLASSIFIED

    def test_case_insensitive(self) -> None:
        """Should parse case-insensitively."""
        result = _parse_enum("RISING", Phase, Phase.UNCLASSIFIED)
        assert result == Phase.RISING


class TestParseOptionalEnum:
    """Tests for _parse_optional_enum helper."""

    def test_parses_valid_value(self) -> None:
        """Should parse a valid enum value."""
        result = _parse_optional_enum("analytical", VoiceRegister)
        assert result == VoiceRegister.ANALYTICAL

    def test_returns_none_for_none(self) -> None:
        """Should return None for None input."""
        result = _parse_optional_enum(None, VoiceRegister)
        assert result is None

    def test_returns_none_for_unknown(self) -> None:
        """Should return None for unknown value."""
        result = _parse_optional_enum("xyz", VoiceRegister)
        assert result is None


class TestParseDosage:
    """Tests for _parse_dosage helper."""

    def test_parses_medicine(self) -> None:
        """Should parse 'medicine' correctly."""
        assert _parse_dosage("medicine") == Dosage.MEDICINE

    def test_parses_toxic(self) -> None:
        """Should parse 'toxic' correctly."""
        assert _parse_dosage("toxic") == Dosage.TOXIC

    def test_ambiguous_markers(self) -> None:
        """Ambiguous markers should map to Dosage.AMBIGUOUS."""
        for marker in ("ambiguous", "unclear", "mixed", "both"):
            assert _parse_dosage(marker) == Dosage.AMBIGUOUS

    def test_none_returns_unclassified(self) -> None:
        """None should return Dosage.UNCLASSIFIED."""
        assert _parse_dosage(None) == Dosage.UNCLASSIFIED

    def test_unknown_returns_unclassified(self) -> None:
        """Unknown value should return Dosage.UNCLASSIFIED."""
        assert _parse_dosage("xyz") == Dosage.UNCLASSIFIED


class TestStripCodeFences:
    """Tests for _strip_code_fences helper."""

    def test_no_fences_unchanged(self) -> None:
        """Text without fences should pass through unchanged."""
        assert _strip_code_fences("hello") == "hello"

    def test_strips_yaml_fences(self) -> None:
        """Should strip ```yaml fences."""
        text = "```yaml\nkey: value\n```"
        assert "key: value" in _strip_code_fences(text)
        assert "```" not in _strip_code_fences(text)

    def test_strips_plain_fences(self) -> None:
        """Should strip plain ``` fences."""
        text = "```\nkey: value\n```"
        result = _strip_code_fences(text)
        assert "key: value" in result
        assert "```" not in result


class TestBatchStats:
    """Tests for BatchStats dataclass."""

    def test_defaults(self) -> None:
        """BatchStats should have zero defaults."""
        stats = BatchStats()
        assert stats.total == 0
        assert stats.classified == 0
        assert stats.failed == 0

    def test_custom_values(self) -> None:
        """BatchStats should accept custom values."""
        stats = BatchStats(total=10, classified=8, failed=2)
        assert stats.total == 10
        assert stats.classified == 8
        assert stats.failed == 2


# ---- AnthropicProvider ----


def _set_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the ANTHROPIC_API_KEY and CREEK_ANTHROPIC_CONSENT env vars.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")


def _make_mock_anthropic_client(response_text: str) -> MagicMock:
    """Build a mock anthropic.Anthropic client returning *response_text*.

    Args:
        response_text: The text to embed in a single content block.

    Returns:
        A ``MagicMock`` whose ``messages.create`` returns a response.
    """
    mock_block = MagicMock()
    mock_block.text = response_text
    mock_response = MagicMock()
    mock_response.content = [mock_block]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


class TestAnthropicProviderInit:
    """Tests for AnthropicProvider initialization."""

    def test_raises_when_api_key_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """__init__ raises when ANTHROPIC_API_KEY is not set."""
        monkeypatch.delenv(AnthropicProvider.API_KEY_ENV, raising=False)
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")
        config = LLMConfig(provider="anthropic")
        with pytest.raises(RuntimeError, match=AnthropicProvider.API_KEY_ENV):
            AnthropicProvider(config)

    def test_raises_when_api_key_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """__init__ raises when ANTHROPIC_API_KEY is whitespace only."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "   ")
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")
        config = LLMConfig(provider="anthropic")
        with pytest.raises(RuntimeError, match=AnthropicProvider.API_KEY_ENV):
            AnthropicProvider(config)

    def test_raises_when_consent_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """__init__ raises when CREEK_ANTHROPIC_CONSENT is not set."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)
        config = LLMConfig(provider="anthropic")
        with pytest.raises(RuntimeError, match="consent"):
            AnthropicProvider(config)

    def test_raises_when_consent_not_truthy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """__init__ raises when consent is set to a falsy value."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "no")
        config = LLMConfig(provider="anthropic")
        with pytest.raises(RuntimeError, match="consent"):
            AnthropicProvider(config)

    def test_succeeds_with_valid_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """__init__ stores the config when env vars are set."""
        _set_anthropic_env(monkeypatch)
        config = LLMConfig(provider="anthropic")
        provider = AnthropicProvider(config)
        assert provider.config is config

    def test_accepts_true_as_consent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Consent value ``true`` should be accepted."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "TRUE")
        AnthropicProvider(LLMConfig(provider="anthropic"))

    def test_accepts_yes_as_consent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Consent value ``yes`` should be accepted."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "yes")
        AnthropicProvider(LLMConfig(provider="anthropic"))

    def test_sdk_client_not_built_on_init(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The SDK client should be created lazily on first use."""
        _set_anthropic_env(monkeypatch)
        provider = AnthropicProvider(LLMConfig(provider="anthropic"))
        assert provider._client is None


class TestAnthropicProviderModel:
    """Tests for AnthropicProvider.model resolution."""

    def test_defaults_when_config_uses_ollama_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Ollama default ``mistral`` should fall back to Anthropic default."""
        _set_anthropic_env(monkeypatch)
        provider = AnthropicProvider(LLMConfig(provider="anthropic"))
        assert provider.model == AnthropicProvider.DEFAULT_MODEL

    def test_uses_config_model_when_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit config model value should be honoured."""
        _set_anthropic_env(monkeypatch)
        config = LLMConfig(provider="anthropic", model="claude-custom-model")
        provider = AnthropicProvider(config)
        assert provider.model == "claude-custom-model"

    def test_default_model_is_claude_sonnet_4_5(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The default model should be the documented Claude Sonnet 4.5."""
        _set_anthropic_env(monkeypatch)
        assert AnthropicProvider.DEFAULT_MODEL == "claude-sonnet-4-5-20250929"


class TestAnthropicProviderCall:
    """Tests for AnthropicProvider.call with a mocked SDK."""

    def test_call_uses_sdk_messages_create(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """call should invoke ``client.messages.create`` with the prompt."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        with patch("anthropic.Anthropic", return_value=mock_client):
            provider = AnthropicProvider(LLMConfig(provider="anthropic"))
            result = provider.call("hello prompt")
        assert result == _VALID_YAML_RESPONSE
        mock_client.messages.create.assert_called_once()
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["model"] == AnthropicProvider.DEFAULT_MODEL
        assert kwargs["max_tokens"] == AnthropicProvider.MAX_TOKENS
        assert kwargs["messages"] == [
            {"role": "user", "content": "hello prompt"},
        ]

    def test_call_concatenates_multiple_text_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple content blocks should be concatenated in order."""
        _set_anthropic_env(monkeypatch)
        block_a = MagicMock()
        block_a.text = "part-a"
        block_b = MagicMock()
        block_b.text = "part-b"
        response = MagicMock()
        response.content = [block_a, block_b]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response
        with patch("anthropic.Anthropic", return_value=mock_client):
            provider = AnthropicProvider(LLMConfig(provider="anthropic"))
            result = provider.call("prompt")
        assert result == "part-apart-b"

    def test_call_ignores_non_text_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Blocks without a string ``text`` attribute should be skipped."""
        _set_anthropic_env(monkeypatch)
        text_block = MagicMock()
        text_block.text = "keep"
        tool_block = MagicMock(spec=[])  # no text attribute
        response = MagicMock()
        response.content = [tool_block, text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response
        with patch("anthropic.Anthropic", return_value=mock_client):
            provider = AnthropicProvider(LLMConfig(provider="anthropic"))
            result = provider.call("prompt")
        assert result == "keep"

    def test_call_honours_custom_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit config.model value should reach the SDK."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        config = LLMConfig(provider="anthropic", model="my-claude-variant")
        with patch("anthropic.Anthropic", return_value=mock_client):
            AnthropicProvider(config).call("prompt")
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "my-claude-variant"

    def test_call_wraps_sdk_errors_without_leaking_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SDK errors should raise RuntimeError with no API key exposure."""
        import anthropic

        _set_anthropic_env(monkeypatch)
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APIError(
            message="boom",
            request=MagicMock(),
            body=None,
        )
        with (
            patch("anthropic.Anthropic", return_value=mock_client),
            pytest.raises(RuntimeError) as exc_info,
        ):
            provider = AnthropicProvider(LLMConfig(provider="anthropic"))
            provider.call("prompt")
        assert "sk-test-not-real" not in str(exc_info.value)

    def test_client_cached_across_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The SDK client should only be instantiated once."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        with patch("anthropic.Anthropic", return_value=mock_client) as mock_ctor:
            provider = AnthropicProvider(LLMConfig(provider="anthropic"))
            provider.call("p1")
            provider.call("p2")
        assert mock_ctor.call_count == 1


# ---- LLMClassifier with Anthropic provider ----


class TestLLMClassifierAnthropicDispatch:
    """Tests for provider dispatching in LLMClassifier."""

    def test_init_logs_warning_when_provider_is_anthropic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Selecting the anthropic provider should log the cloud warning."""
        _set_anthropic_env(monkeypatch)
        with caplog.at_level(logging.WARNING):
            LLMClassifier(config=LLMConfig(provider="anthropic"))
        assert any(
            "anthropic" in r.message.lower() and "cloud" in r.message.lower()
            for r in caplog.records
        )

    def test_init_does_not_warn_for_ollama(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Default ollama provider should not emit the cloud warning."""
        with caplog.at_level(logging.WARNING):
            LLMClassifier(config=LLMConfig())
        assert all(ANTHROPIC_CLOUD_WARNING not in r.message for r in caplog.records)

    def test_available_true_when_anthropic_env_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """available should be True when both env vars are present."""
        _set_anthropic_env(monkeypatch)
        classifier = LLMClassifier(config=LLMConfig(provider="anthropic"))
        assert classifier.available is True

    def test_available_false_when_api_key_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """available should be False when the API key is absent."""
        monkeypatch.delenv(AnthropicProvider.API_KEY_ENV, raising=False)
        monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")
        classifier = LLMClassifier(config=LLMConfig(provider="anthropic"))
        assert classifier.available is False

    def test_available_false_when_consent_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """available should be False when consent is not granted."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)
        classifier = LLMClassifier(config=LLMConfig(provider="anthropic"))
        assert classifier.available is False

    def test_classify_dispatches_to_anthropic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """classify should route to the anthropic provider when selected."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        with patch("anthropic.Anthropic", return_value=mock_client):
            classifier = LLMClassifier(
                config=LLMConfig(provider="anthropic"),
            )
            result = classifier.classify(_make_fragment(title="Test"))
        assert result.frequency.primary == Frequency.F3
        mock_client.messages.create.assert_called_once()

    def test_classify_uses_same_prompt_for_both_providers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anthropic path should send CLASSIFICATION_PROMPT-formatted text."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        with patch("anthropic.Anthropic", return_value=mock_client):
            classifier = LLMClassifier(
                config=LLMConfig(provider="anthropic"),
            )
            classifier.classify(
                _make_fragment(title="My Title"),
                content="Body text",
            )
        kwargs = mock_client.messages.create.call_args.kwargs
        prompt = kwargs["messages"][0]["content"]
        assert "My Title" in prompt
        assert "Body text" in prompt
        assert "Frequency" in prompt

    def test_classify_batch_dispatches_to_anthropic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """classify_batch should call the anthropic SDK for each fragment."""
        _set_anthropic_env(monkeypatch)
        mock_client = _make_mock_anthropic_client(_VALID_YAML_RESPONSE)
        with (
            patch("anthropic.Anthropic", return_value=mock_client),
            patch("creek.classify.llm.time.sleep"),
        ):
            classifier = LLMClassifier(
                config=LLMConfig(provider="anthropic"),
            )
            frags = [_make_fragment(title=f"F{i}") for i in range(3)]
            results = classifier.classify_batch(frags, progress=False)
        assert len(results) == 3
        assert mock_client.messages.create.call_count == 3
        for res in results:
            assert res.frequency.primary == Frequency.F3

    def test_classify_retries_on_runtime_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """classify should retry when the provider raises RuntimeError."""
        _set_anthropic_env(monkeypatch)
        mock_client = MagicMock()
        good_block = MagicMock()
        good_block.text = _VALID_YAML_RESPONSE
        good_response = MagicMock()
        good_response.content = [good_block]
        import anthropic

        mock_client.messages.create.side_effect = [
            anthropic.APIError(
                message="rate limited",
                request=MagicMock(),
                body=None,
            ),
            good_response,
        ]
        with (
            patch("anthropic.Anthropic", return_value=mock_client),
            patch("creek.classify.llm.time.sleep"),
        ):
            classifier = LLMClassifier(
                config=LLMConfig(provider="anthropic"),
            )
            result = classifier.classify(_make_fragment())
        assert result.frequency.primary == Frequency.F3
        assert mock_client.messages.create.call_count == 2

    def test_ollama_path_unchanged_by_anthropic_changes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Default ollama provider continues to use _call_ollama."""
        with patch.object(
            LLMClassifier, "_call_ollama", return_value=_VALID_YAML_RESPONSE
        ) as mock_call:
            classifier = _make_classifier_available(
                LLMClassifier(config=LLMConfig()),
            )
            classifier.classify(_make_fragment())
        mock_call.assert_called_once()


# ---- Integration test (requires running Ollama) ----


class TestLLMClassifierIntegration:
    """Integration tests for LLMClassifier with real Ollama."""

    @pytest.mark.integration
    def test_classify_with_real_ollama(self) -> None:
        """classify should work with a real Ollama instance."""
        config = LLMConfig()
        classifier = LLMClassifier(config=config)
        if not classifier.available:
            pytest.skip("Ollama not available")
        frag = _make_fragment(title="Survival and safety concerns")
        result = classifier.classify(
            frag,
            content="I am worried about my safety and security",
        )
        assert isinstance(result, Fragment)


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
