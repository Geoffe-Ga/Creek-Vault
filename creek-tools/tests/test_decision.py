"""Tests for creek.decision — decision support layer.

Tests cover DecisionDetector, DetectedDecision, and all detection methods
including signal matching, draft decision creation, and note generation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from creek.decision import DecisionDetector, DetectedDecision
from creek.decision.detector import DECISION_SIGNALS
from creek.models import (
    Decision,
    DecisionStatus,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    SourcePlatform,
    WavelengthClassification,
)

# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault with decision directories."""
    (tmp_path / "08-Decisions").mkdir()
    (tmp_path / "08-Decisions" / "Active").mkdir()
    return tmp_path


@pytest.fixture()
def detector(vault: Path) -> DecisionDetector:
    """Create a DecisionDetector instance."""
    return DecisionDetector(vault)


def _make_fragment(
    title: str = "Test Fragment",
    frag_id: str = "",
    frequency: Frequency = Frequency.UNCLASSIFIED,
    phase: str = "unclassified",
) -> Fragment:
    """Helper to create a fragment for testing."""
    frag = Fragment(
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(phase=phase),
    )
    if frag_id:
        frag = frag.model_copy(update={"id": frag_id})
    return frag


# ---- DECISION_SIGNALS Tests ----


class TestDecisionSignals:
    """Tests for the DECISION_SIGNALS list."""

    def test_signals_not_empty(self) -> None:
        """Should have at least one signal keyword."""
        assert len(DECISION_SIGNALS) > 0

    def test_signals_are_lowercase(self) -> None:
        """All signals should be lowercase."""
        for signal in DECISION_SIGNALS:
            assert signal == signal.lower()

    def test_contains_key_phrases(self) -> None:
        """Should contain core decision keywords."""
        assert "should i" in DECISION_SIGNALS
        assert "trying to decide" in DECISION_SIGNALS
        assert "weighing options" in DECISION_SIGNALS


# ---- DetectedDecision Tests ----


class TestDetectedDecision:
    """Tests for DetectedDecision model."""

    def test_creation(self) -> None:
        """Should create with required fields."""
        d = DetectedDecision(
            fragment_id="frag-001",
            fragment_title="Test",
        )
        assert d.fragment_id == "frag-001"
        assert d.signal_count == 0
        assert d.matched_signals == []

    def test_with_signals(self) -> None:
        """Should store matched signals."""
        d = DetectedDecision(
            fragment_id="frag-001",
            fragment_title="Test",
            matched_signals=["should i", "deciding between"],
            signal_count=2,
        )
        assert d.signal_count == 2
        assert len(d.matched_signals) == 2


# ---- DecisionDetector Tests ----


class TestDecisionDetectorInit:
    """Tests for DecisionDetector.__init__."""

    def test_stores_vault_path(self, vault: Path) -> None:
        """Should store the vault path."""
        d = DecisionDetector(vault)
        assert d.vault_path == vault

    def test_default_threshold(self, vault: Path) -> None:
        """Default threshold should be 1."""
        d = DecisionDetector(vault)
        assert d.signal_threshold == 1

    def test_custom_threshold(self, vault: Path) -> None:
        """Should accept custom threshold."""
        d = DecisionDetector(vault, signal_threshold=3)
        assert d.signal_threshold == 3


class TestDetect:
    """Tests for DecisionDetector.detect."""

    def test_detects_decision_content(self, detector: DecisionDetector) -> None:
        """Should detect fragments with decision keywords."""
        frag = _make_fragment(frag_id="frag-001")
        content_map = {"frag-001": "I'm trying to decide whether to move."}
        result = detector.detect([frag], content_map)
        assert len(result) == 1
        assert result[0].fragment_id == "frag-001"

    def test_detects_title_signals(self, detector: DecisionDetector) -> None:
        """Should detect signals in fragment titles."""
        frag = _make_fragment(
            title="Should I change careers?",
            frag_id="frag-002",
        )
        result = detector.detect([frag], {})
        assert len(result) == 1

    def test_skips_non_decision_content(self, detector: DecisionDetector) -> None:
        """Should skip fragments without decision signals."""
        frag = _make_fragment(frag_id="frag-003")
        content_map = {"frag-003": "The weather is nice today."}
        result = detector.detect([frag], content_map)
        assert len(result) == 0

    def test_respects_threshold(self, vault: Path) -> None:
        """Should respect the signal threshold."""
        d = DecisionDetector(vault, signal_threshold=3)
        frag = _make_fragment(frag_id="frag-004")
        content_map = {"frag-004": "Should I decide? Just one signal here."}
        result = d.detect([frag], content_map)
        assert len(result) == 0

    def test_captures_frequency_context(self, detector: DecisionDetector) -> None:
        """Should capture the fragment's frequency context."""
        frag = _make_fragment(
            frag_id="frag-005",
            frequency=Frequency.F4,
        )
        content_map = {"frag-005": "Should I follow the rules?"}
        result = detector.detect([frag], content_map)
        assert len(result) == 1
        assert "F4" in result[0].frequency_context

    def test_empty_fragments(self, detector: DecisionDetector) -> None:
        """Empty fragment list returns empty result."""
        assert detector.detect([], {}) == []

    def test_multiple_signals_counted(self, detector: DecisionDetector) -> None:
        """Multiple signals in one fragment should be counted."""
        frag = _make_fragment(frag_id="frag-006")
        content_map = {
            "frag-006": (
                "I'm trying to decide and weighing options about this dilemma."
            )
        }
        result = detector.detect([frag], content_map)
        assert len(result) == 1
        assert result[0].signal_count >= 2


class TestCreateDraftDecisions:
    """Tests for DecisionDetector.create_draft_decisions."""

    def test_creates_decisions(self, detector: DecisionDetector) -> None:
        """Should create Decision instances from detected decisions."""
        detected = [
            DetectedDecision(
                fragment_id="frag-001",
                fragment_title="Career Change",
                matched_signals=["should i"],
                signal_count=1,
                frequency_context=["F4"],
                wavelength_phase="rising",
            )
        ]
        result = detector.create_draft_decisions(detected)
        assert len(result) == 1
        assert isinstance(result[0], Decision)

    def test_decision_has_sensing_status(self, detector: DecisionDetector) -> None:
        """Draft decisions should start in SENSING status."""
        detected = [
            DetectedDecision(
                fragment_id="frag-001",
                fragment_title="Test",
            )
        ]
        result = detector.create_draft_decisions(detected)
        assert result[0].status == DecisionStatus.SENSING

    def test_decision_has_title(self, detector: DecisionDetector) -> None:
        """Decision title should reference the fragment."""
        detected = [
            DetectedDecision(
                fragment_id="frag-001",
                fragment_title="Move Abroad",
            )
        ]
        result = detector.create_draft_decisions(detected)
        assert "Move Abroad" in result[0].title

    def test_decision_preserves_frequency(self, detector: DecisionDetector) -> None:
        """Decision should preserve frequency context."""
        detected = [
            DetectedDecision(
                fragment_id="frag-001",
                fragment_title="Test",
                frequency_context=["F4", "F6"],
            )
        ]
        result = detector.create_draft_decisions(detected)
        freq_values = [str(f) for f in result[0].frequency_context]
        assert "F4" in freq_values
        assert "F6" in freq_values

    def test_empty_detected(self, detector: DecisionDetector) -> None:
        """Empty detected list returns empty decisions."""
        assert detector.create_draft_decisions([]) == []


class TestGenerateDecisionNotes:
    """Tests for DecisionDetector.generate_decision_notes."""

    def test_generates_files(self, detector: DecisionDetector, vault: Path) -> None:
        """Should generate markdown files in 08-Decisions/Active/."""
        decisions = [
            Decision(title="Decision: Test Choice"),
        ]
        result = detector.generate_decision_notes(decisions)
        assert len(result) == 1
        assert result[0].is_file()

    def test_file_in_active_dir(self, detector: DecisionDetector, vault: Path) -> None:
        """Files should be in the Active subdirectory."""
        decisions = [Decision(title="Decision: Test")]
        result = detector.generate_decision_notes(decisions)
        assert result[0].parent == vault / "08-Decisions" / "Active"

    def test_file_has_frontmatter(self, detector: DecisionDetector) -> None:
        """Generated files should have YAML frontmatter."""
        decisions = [Decision(title="Decision: Test")]
        result = detector.generate_decision_notes(decisions)
        content = result[0].read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: decision" in content

    def test_file_has_status(self, detector: DecisionDetector) -> None:
        """Generated file should mention the decision status."""
        decisions = [Decision(title="Decision: Test")]
        result = detector.generate_decision_notes(decisions)
        content = result[0].read_text(encoding="utf-8")
        assert "sensing" in content

    def test_file_has_context_sections(self, detector: DecisionDetector) -> None:
        """Generated file should have context sections."""
        decisions = [Decision(title="Decision: Test")]
        result = detector.generate_decision_notes(decisions)
        content = result[0].read_text(encoding="utf-8")
        assert "## Context" in content
        assert "Related Threads" in content
        assert "Wavelength Phase" in content

    def test_multiple_decisions(self, detector: DecisionDetector) -> None:
        """Should generate one file per decision."""
        decisions = [
            Decision(title="Decision: A"),
            Decision(title="Decision: B"),
        ]
        result = detector.generate_decision_notes(decisions)
        assert len(result) == 2

    def test_creates_active_dir_if_missing(self, tmp_path: Path) -> None:
        """Should create Active/ dir if missing."""
        d = DecisionDetector(tmp_path)
        decisions = [Decision(title="Decision: Test")]
        result = d.generate_decision_notes(decisions)
        assert result[0].is_file()
        assert (tmp_path / "08-Decisions" / "Active").is_dir()


class TestDecisionDetectorRun:
    """Tests for DecisionDetector.run end-to-end."""

    def test_run_produces_notes(self, detector: DecisionDetector) -> None:
        """run() should detect and generate decision notes."""
        frag = _make_fragment(
            title="Should I quit my job?",
            frag_id="frag-001",
        )
        content_map = {"frag-001": "I'm trying to decide."}
        result = detector.run([frag], content_map)
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)
        assert len(result) >= 1

    def test_run_with_no_decisions(self, detector: DecisionDetector) -> None:
        """run() with no decision content returns empty list."""
        frag = _make_fragment(frag_id="frag-001")
        content_map = {"frag-001": "Today the sun was bright."}
        result = detector.run([frag], content_map)
        assert result == []

    def test_run_with_empty_fragments(self, detector: DecisionDetector) -> None:
        """run() with empty fragments returns empty list."""
        result = detector.run([], {})
        assert result == []


# ---- Package Export Tests ----


class TestPackageExports:
    """Tests for creek.decision package exports."""

    def test_decision_detector_exported(self) -> None:
        """DecisionDetector should be importable from creek.decision."""
        from creek.decision import DecisionDetector as DetectorImport

        assert DetectorImport is DecisionDetector

    def test_detected_decision_exported(self) -> None:
        """DetectedDecision should be importable from creek.decision."""
        from creek.decision import DetectedDecision as DetectedImport

        assert DetectedImport is DetectedDecision
