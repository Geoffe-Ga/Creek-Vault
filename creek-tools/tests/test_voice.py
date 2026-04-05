"""Tests for creek.voice — voice analysis pipeline.

Tests cover VoiceAnalyzer, VoicePattern, VoiceProfile, and all
analysis methods including exemplar collection, pattern extraction,
profile building, and note generation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
)
from creek.voice import VoiceAnalyzer, VoicePattern, VoiceProfile
from creek.voice.analyzer import (
    _ELLIPSIS,
    _EM_DASH,
    _PARENTHETICAL,
    _average_sentence_length,
    _count_pattern,
    _extract_distinctive_words,
)

# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault with 07-Voice directory."""
    (tmp_path / "07-Voice").mkdir()
    return tmp_path


@pytest.fixture()
def analyzer(vault: Path) -> VoiceAnalyzer:
    """Create a VoiceAnalyzer instance."""
    return VoiceAnalyzer(vault)


def _make_fragment(
    title: str = "Test Fragment",
    register: VoiceRegister | None = VoiceRegister.ANALYTICAL,
    confidence: Confidence | None = Confidence.SETTLED,
    frag_id: str = "",
) -> Fragment:
    """Helper to create a fragment with voice classification."""
    frag = Fragment(
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        voice=VoiceClassification(
            voice_register=register,
            confidence=confidence,
        ),
    )
    if frag_id:
        frag = frag.model_copy(update={"id": frag_id})
    return frag


# ---- Helper Function Tests ----


class TestAverageSentenceLength:
    """Tests for _average_sentence_length."""

    def test_simple_sentences(self) -> None:
        """Two simple sentences should average correctly."""
        text = "Hello world. How are you today."
        result = _average_sentence_length(text)
        assert result > 0

    def test_empty_text(self) -> None:
        """Empty text returns 0.0."""
        assert _average_sentence_length("") == 0.0

    def test_single_sentence(self) -> None:
        """A single sentence without trailing period."""
        text = "Just one sentence here"
        result = _average_sentence_length(text)
        assert result == 4.0


class TestCountPattern:
    """Tests for _count_pattern."""

    def test_em_dash_count(self) -> None:
        """Should count em-dashes."""
        text = "Hello\u2014world\u2014test"
        assert _count_pattern(text, _EM_DASH) == 2

    def test_ellipsis_count(self) -> None:
        """Should count ellipses."""
        text = "Wait... really... yes"
        assert _count_pattern(text, _ELLIPSIS) == 2

    def test_parenthetical_count(self) -> None:
        """Should count parentheticals."""
        text = "Hello (world) and (test) here"
        assert _count_pattern(text, _PARENTHETICAL) == 2

    def test_no_matches(self) -> None:
        """Empty result for plain text."""
        assert _count_pattern("Hello world", _EM_DASH) == 0


class TestExtractDistinctiveWords:
    """Tests for _extract_distinctive_words."""

    def test_returns_list(self) -> None:
        """Should return a list of strings."""
        result = _extract_distinctive_words(["hello world test"])
        assert isinstance(result, list)

    def test_excludes_stop_words(self) -> None:
        """Should not include common stop words."""
        result = _extract_distinctive_words(["the and for with"])
        assert "the" not in result
        assert "and" not in result

    def test_respects_top_n(self) -> None:
        """Should respect the top_n parameter."""
        result = _extract_distinctive_words(
            ["alpha bravo charlie delta echo foxtrot"],
            top_n=3,
        )
        assert len(result) <= 3

    def test_empty_input(self) -> None:
        """Empty input should return empty list."""
        assert _extract_distinctive_words([]) == []


# ---- VoicePattern Tests ----


class TestVoicePattern:
    """Tests for VoicePattern model."""

    def test_default_values(self) -> None:
        """Default pattern has zero values."""
        pattern = VoicePattern()
        assert pattern.avg_sentence_length == 0.0
        assert pattern.exemplar_count == 0
        assert pattern.distinctive_words == []

    def test_custom_values(self) -> None:
        """Pattern accepts custom values."""
        pattern = VoicePattern(
            avg_sentence_length=15.2,
            em_dash_frequency=2.3,
            exemplar_count=10,
        )
        assert pattern.avg_sentence_length == 15.2
        assert pattern.em_dash_frequency == 2.3
        assert pattern.exemplar_count == 10


# ---- VoiceProfile Tests ----


class TestVoiceProfile:
    """Tests for VoiceProfile model."""

    def test_creation(self) -> None:
        """Profile should store register and pattern."""
        profile = VoiceProfile(voice_register_name="analytical")
        assert profile.voice_register_name == "analytical"
        assert isinstance(profile.pattern, VoicePattern)
        assert profile.exemplar_titles == []

    def test_with_titles(self) -> None:
        """Profile should store exemplar titles."""
        profile = VoiceProfile(
            voice_register_name="confessional",
            exemplar_titles=["Frag A", "Frag B"],
        )
        assert len(profile.exemplar_titles) == 2


# ---- VoiceAnalyzer Tests ----


class TestVoiceAnalyzerInit:
    """Tests for VoiceAnalyzer.__init__."""

    def test_stores_vault_path(self, vault: Path) -> None:
        """Should store the vault path."""
        a = VoiceAnalyzer(vault)
        assert a.vault_path == vault


class TestCollectExemplars:
    """Tests for VoiceAnalyzer.collect_exemplars."""

    def test_filters_by_confidence(self, analyzer: VoiceAnalyzer) -> None:
        """Only settled/conviction fragments are collected."""
        frags = [
            _make_fragment(confidence=Confidence.SETTLED),
            _make_fragment(confidence=Confidence.MUSING),
            _make_fragment(confidence=Confidence.CONVICTION),
        ]
        result = analyzer.collect_exemplars(frags)
        total = sum(len(v) for v in result.values())
        assert total == 2

    def test_groups_by_register(self, analyzer: VoiceAnalyzer) -> None:
        """Fragments should be grouped by voice register."""
        frags = [
            _make_fragment(register=VoiceRegister.ANALYTICAL),
            _make_fragment(register=VoiceRegister.CONFESSIONAL),
            _make_fragment(register=VoiceRegister.ANALYTICAL),
        ]
        result = analyzer.collect_exemplars(frags)
        assert len(result.get("analytical", [])) == 2
        assert len(result.get("confessional", [])) == 1

    def test_skips_none_register(self, analyzer: VoiceAnalyzer) -> None:
        """Fragments without a register are skipped."""
        frags = [_make_fragment(register=None)]
        result = analyzer.collect_exemplars(frags)
        assert len(result) == 0

    def test_empty_fragments(self, analyzer: VoiceAnalyzer) -> None:
        """Empty list returns empty dict."""
        assert analyzer.collect_exemplars([]) == {}


class TestExtractPatterns:
    """Tests for VoiceAnalyzer.extract_patterns."""

    def test_returns_patterns_per_register(self, analyzer: VoiceAnalyzer) -> None:
        """Should return one pattern per register."""
        content = {
            "analytical": ["This is a test. Another sentence here."],
            "confessional": ["I confess... this is hard\u2014really hard."],
        }
        result = analyzer.extract_patterns(content)
        assert "analytical" in result
        assert "confessional" in result

    def test_empty_content_returns_empty_pattern(self, analyzer: VoiceAnalyzer) -> None:
        """Empty content list returns default pattern."""
        result = analyzer.extract_patterns({"test": []})
        assert result["test"].exemplar_count == 0

    def test_counts_punctuation(self, analyzer: VoiceAnalyzer) -> None:
        """Should detect em-dashes and ellipses."""
        content = {
            "raw": ["Hello\u2014world... and (aside) more..."],
        }
        result = analyzer.extract_patterns(content)
        assert result["raw"].em_dash_frequency >= 1.0
        assert result["raw"].ellipsis_frequency >= 1.0
        assert result["raw"].parenthetical_frequency >= 1.0


class TestBuildProfiles:
    """Tests for VoiceAnalyzer.build_profiles."""

    def test_returns_profiles(self, analyzer: VoiceAnalyzer) -> None:
        """Should return a list of VoiceProfile objects."""
        frag = _make_fragment(frag_id="frag-001")
        content_map = {"frag-001": "This is test content."}
        result = analyzer.build_profiles([frag], content_map)
        assert isinstance(result, list)
        assert all(isinstance(p, VoiceProfile) for p in result)

    def test_profile_has_register(self, analyzer: VoiceAnalyzer) -> None:
        """Each profile should have a register name."""
        frag = _make_fragment(frag_id="frag-001")
        content_map = {"frag-001": "Analytical content here."}
        result = analyzer.build_profiles([frag], content_map)
        assert len(result) == 1
        assert result[0].voice_register_name == "analytical"

    def test_empty_fragments(self, analyzer: VoiceAnalyzer) -> None:
        """Empty fragments list returns empty profiles."""
        assert analyzer.build_profiles([], {}) == []


class TestGenerateProfileNotes:
    """Tests for VoiceAnalyzer.generate_profile_notes."""

    def test_generates_files(self, analyzer: VoiceAnalyzer, vault: Path) -> None:
        """Should generate markdown files in 07-Voice/."""
        profiles = [
            VoiceProfile(
                voice_register_name="analytical",
                pattern=VoicePattern(exemplar_count=5),
                exemplar_titles=["Frag A"],
            ),
        ]
        result = analyzer.generate_profile_notes(profiles)
        assert len(result) == 1
        assert result[0].is_file()
        assert result[0].parent == vault / "07-Voice"

    def test_file_has_frontmatter(self, analyzer: VoiceAnalyzer) -> None:
        """Generated files should have YAML frontmatter."""
        profiles = [VoiceProfile(voice_register_name="playful")]
        result = analyzer.generate_profile_notes(profiles)
        content = result[0].read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: voice-profile" in content

    def test_file_has_register_name(self, analyzer: VoiceAnalyzer) -> None:
        """Generated file should mention the register name."""
        profiles = [VoiceProfile(voice_register_name="confessional")]
        result = analyzer.generate_profile_notes(profiles)
        content = result[0].read_text(encoding="utf-8")
        assert "Confessional" in content

    def test_multiple_profiles(self, analyzer: VoiceAnalyzer) -> None:
        """Should generate one file per profile."""
        profiles = [
            VoiceProfile(voice_register_name="analytical"),
            VoiceProfile(voice_register_name="raw"),
        ]
        result = analyzer.generate_profile_notes(profiles)
        assert len(result) == 2

    def test_creates_voice_dir_if_missing(self, tmp_path: Path) -> None:
        """Should create 07-Voice/ if it doesn't exist."""
        a = VoiceAnalyzer(tmp_path)
        profiles = [VoiceProfile(voice_register_name="playful")]
        result = a.generate_profile_notes(profiles)
        assert result[0].is_file()
        assert (tmp_path / "07-Voice").is_dir()


class TestVoiceAnalyzerRun:
    """Tests for VoiceAnalyzer.run end-to-end."""

    def test_run_produces_notes(self, analyzer: VoiceAnalyzer) -> None:
        """run() should produce profile note files."""
        frag = _make_fragment(frag_id="frag-001")
        content_map = {"frag-001": "Deep analytical content here."}
        result = analyzer.run([frag], content_map)
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)

    def test_run_with_no_fragments(self, analyzer: VoiceAnalyzer) -> None:
        """run() with empty fragments returns empty list."""
        result = analyzer.run([], {})
        assert result == []


# ---- Package Export Tests ----


class TestPackageExports:
    """Tests for creek.voice package exports."""

    def test_voice_analyzer_exported(self) -> None:
        """VoiceAnalyzer should be importable from creek.voice."""
        from creek.voice import VoiceAnalyzer as VoiceAnalyzerImport

        assert VoiceAnalyzerImport is VoiceAnalyzer

    def test_voice_pattern_exported(self) -> None:
        """VoicePattern should be importable from creek.voice."""
        from creek.voice import VoicePattern as VoicePatternImport

        assert VoicePatternImport is VoicePattern

    def test_voice_profile_exported(self) -> None:
        """VoiceProfile should be importable from creek.voice."""
        from creek.voice import VoiceProfile as VoiceProfileImport

        assert VoiceProfileImport is VoiceProfile
