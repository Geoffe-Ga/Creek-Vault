"""Tests for creek.wavelength — wavelength tracking system.

Tests cover WavelengthTracker, PhaseTransition, DosageTrend,
WavelengthReport, and all tracking methods including phase analysis,
transition detection, dosage analysis, and report generation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from creek.models import (
    Dosage,
    Fragment,
    FragmentSource,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)
from creek.wavelength import PhaseTransition, WavelengthReport, WavelengthTracker
from creek.wavelength.tracker import DosageTrend

# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault with wavelength directories."""
    (tmp_path / "05-Wavelength").mkdir()
    (tmp_path / "05-Wavelength" / "Phase-Maps").mkdir()
    return tmp_path


@pytest.fixture()
def tracker(vault: Path) -> WavelengthTracker:
    """Create a WavelengthTracker instance."""
    return WavelengthTracker(vault)


def _make_fragment(
    phase: Phase = Phase.RISING,
    dosage: Dosage = Dosage.MEDICINE,
    created_date: datetime | None = None,
) -> Fragment:
    """Helper to create a fragment with wavelength classification."""
    if created_date is None:
        created_date = datetime.now()
    return Fragment(
        title="Test Fragment",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        wavelength=WavelengthClassification(phase=phase, dosage=dosage),
        created=created_date,
    )


# ---- Model Tests ----


class TestPhaseTransition:
    """Tests for PhaseTransition model."""

    def test_creation(self) -> None:
        """Should create a transition with required fields."""
        t = PhaseTransition(
            from_phase="rising",
            to_phase="peaking",
            transition_date=date.today(),
        )
        assert t.from_phase == "rising"
        assert t.to_phase == "peaking"
        assert t.fragment_count == 0


class TestDosageTrend:
    """Tests for DosageTrend model."""

    def test_default_trend(self) -> None:
        """Default trend should be balanced."""
        d = DosageTrend(
            window_start=date.today(),
            window_end=date.today(),
        )
        assert d.trend == "balanced"

    def test_custom_counts(self) -> None:
        """Should accept custom dosage counts."""
        d = DosageTrend(
            window_start=date.today(),
            window_end=date.today(),
            medicine_count=10,
            toxic_count=2,
            trend="medicine",
        )
        assert d.medicine_count == 10
        assert d.toxic_count == 2


class TestWavelengthReport:
    """Tests for WavelengthReport model."""

    def test_default_report(self) -> None:
        """Default report has unclassified phase."""
        r = WavelengthReport(
            period_start=date.today(),
            period_end=date.today(),
        )
        assert r.dominant_phase == "unclassified"
        assert r.fragment_count == 0
        assert r.transitions == []


# ---- WavelengthTracker Tests ----


class TestWavelengthTrackerInit:
    """Tests for WavelengthTracker.__init__."""

    def test_stores_vault_path(self, vault: Path) -> None:
        """Should store the vault path."""
        t = WavelengthTracker(vault)
        assert t.vault_path == vault

    def test_default_window_days(self, vault: Path) -> None:
        """Default window should be 7 days."""
        t = WavelengthTracker(vault)
        assert t.window_days == 7

    def test_custom_window_days(self, vault: Path) -> None:
        """Should accept custom window size."""
        t = WavelengthTracker(vault, window_days=14)
        assert t.window_days == 14


class TestAnalyzePhases:
    """Tests for WavelengthTracker.analyze_phases."""

    def test_counts_phases(self, tracker: WavelengthTracker) -> None:
        """Should count fragment phases."""
        frags = [
            _make_fragment(phase=Phase.RISING),
            _make_fragment(phase=Phase.RISING),
            _make_fragment(phase=Phase.PEAKING),
        ]
        result = tracker.analyze_phases(frags)
        assert result["rising"] == 2
        assert result["peaking"] == 1

    def test_excludes_unclassified(self, tracker: WavelengthTracker) -> None:
        """Should exclude unclassified phases."""
        frags = [
            _make_fragment(phase=Phase.UNCLASSIFIED),
            _make_fragment(phase=Phase.RISING),
        ]
        result = tracker.analyze_phases(frags)
        assert "unclassified" not in result
        assert result.get("rising") == 1

    def test_empty_fragments(self, tracker: WavelengthTracker) -> None:
        """Empty fragments return empty dict."""
        assert tracker.analyze_phases([]) == {}


class TestDetectTransitions:
    """Tests for WavelengthTracker.detect_transitions."""

    def test_detects_transition(self, vault: Path) -> None:
        """Should detect a phase change between windows."""
        tracker = WavelengthTracker(vault, window_days=3)
        now = datetime.now()
        frags = [
            _make_fragment(phase=Phase.RISING, created_date=now - timedelta(days=10)),
            _make_fragment(phase=Phase.RISING, created_date=now - timedelta(days=9)),
            _make_fragment(phase=Phase.PEAKING, created_date=now - timedelta(days=3)),
            _make_fragment(phase=Phase.PEAKING, created_date=now - timedelta(days=2)),
        ]
        result = tracker.detect_transitions(frags)
        assert len(result) >= 1
        assert result[0].from_phase == "rising"
        assert result[0].to_phase == "peaking"

    def test_no_transition_same_phase(self, tracker: WavelengthTracker) -> None:
        """No transition if phase stays the same."""
        frags = [
            _make_fragment(phase=Phase.RISING),
            _make_fragment(phase=Phase.RISING),
        ]
        result = tracker.detect_transitions(frags)
        assert len(result) == 0

    def test_empty_fragments(self, tracker: WavelengthTracker) -> None:
        """Empty fragments return empty list."""
        assert tracker.detect_transitions([]) == []


class TestAnalyzeDosage:
    """Tests for WavelengthTracker.analyze_dosage."""

    def test_counts_dosage_types(self, tracker: WavelengthTracker) -> None:
        """Should count medicine, toxic, and ambiguous dosages."""
        today = date.today()
        frags = [
            _make_fragment(dosage=Dosage.MEDICINE),
            _make_fragment(dosage=Dosage.MEDICINE),
            _make_fragment(dosage=Dosage.TOXIC),
        ]
        result = tracker.analyze_dosage(frags, today, today)
        assert result.medicine_count == 2
        assert result.toxic_count == 1
        assert result.trend == "medicine"

    def test_balanced_trend(self, tracker: WavelengthTracker) -> None:
        """Equal counts should be balanced."""
        today = date.today()
        frags = [
            _make_fragment(dosage=Dosage.MEDICINE),
            _make_fragment(dosage=Dosage.TOXIC),
        ]
        result = tracker.analyze_dosage(frags, today, today)
        assert result.trend == "balanced"

    def test_filters_by_date(self, tracker: WavelengthTracker) -> None:
        """Should only count fragments within the window."""
        today = date.today()
        old = datetime.now() - timedelta(days=30)
        frags = [
            _make_fragment(dosage=Dosage.TOXIC, created_date=old),
            _make_fragment(dosage=Dosage.MEDICINE),
        ]
        result = tracker.analyze_dosage(frags, today, today)
        assert result.medicine_count == 1
        assert result.toxic_count == 0


class TestGenerateReport:
    """Tests for WavelengthTracker.generate_report."""

    def test_returns_report(self, tracker: WavelengthTracker) -> None:
        """Should return a WavelengthReport."""
        today = date.today()
        result = tracker.generate_report([], today, today)
        assert isinstance(result, WavelengthReport)

    def test_report_has_period(self, tracker: WavelengthTracker) -> None:
        """Report should contain the specified period."""
        start = date(2026, 1, 1)
        end = date(2026, 1, 31)
        result = tracker.generate_report([], start, end)
        assert result.period_start == start
        assert result.period_end == end

    def test_report_counts_fragments(self, tracker: WavelengthTracker) -> None:
        """Report should count fragments in the period."""
        today = date.today()
        frags = [_make_fragment(), _make_fragment()]
        result = tracker.generate_report(frags, today, today)
        assert result.fragment_count == 2

    def test_report_finds_dominant_phase(self, tracker: WavelengthTracker) -> None:
        """Report should identify the dominant phase."""
        today = date.today()
        frags = [
            _make_fragment(phase=Phase.RISING),
            _make_fragment(phase=Phase.RISING),
            _make_fragment(phase=Phase.PEAKING),
        ]
        result = tracker.generate_report(frags, today, today)
        assert result.dominant_phase == "rising"


class TestGenerateReportNote:
    """Tests for WavelengthTracker.generate_report_note."""

    def test_generates_file(self, tracker: WavelengthTracker, vault: Path) -> None:
        """Should generate a markdown file."""
        report = WavelengthReport(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 7),
        )
        result = tracker.generate_report_note(report)
        assert result.is_file()
        assert result.suffix == ".md"

    def test_file_in_phase_maps(self, tracker: WavelengthTracker, vault: Path) -> None:
        """File should be in 05-Wavelength/Phase-Maps/."""
        report = WavelengthReport(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 7),
        )
        result = tracker.generate_report_note(report)
        assert result.parent == vault / "05-Wavelength" / "Phase-Maps"

    def test_file_has_frontmatter(self, tracker: WavelengthTracker) -> None:
        """Generated file should have YAML frontmatter."""
        report = WavelengthReport(
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 7),
        )
        result = tracker.generate_report_note(report)
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: wavelength-report" in content

    def test_file_contains_phase_distribution(self, tracker: WavelengthTracker) -> None:
        """Report note should contain phase distribution section."""
        report = WavelengthReport(
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 7),
            phase_counts={"rising": 5, "peaking": 3},
        )
        result = tracker.generate_report_note(report)
        content = result.read_text(encoding="utf-8")
        assert "Phase Distribution" in content
        assert "rising" in content

    def test_creates_dirs_if_missing(self, tmp_path: Path) -> None:
        """Should create Phase-Maps dir if missing."""
        t = WavelengthTracker(tmp_path)
        report = WavelengthReport(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 7),
        )
        result = t.generate_report_note(report)
        assert result.is_file()


class TestWavelengthTrackerRun:
    """Tests for WavelengthTracker.run end-to-end."""

    def test_run_produces_note(self, tracker: WavelengthTracker) -> None:
        """run() should produce a report note file."""
        today = date.today()
        frags = [_make_fragment(phase=Phase.RISING)]
        result = tracker.run(frags, today, today)
        assert isinstance(result, Path)
        assert result.is_file()

    def test_run_with_no_fragments(self, tracker: WavelengthTracker) -> None:
        """run() with empty fragments still produces a report."""
        today = date.today()
        result = tracker.run([], today, today)
        assert result.is_file()


# ---- Package Export Tests ----


class TestPackageExports:
    """Tests for creek.wavelength package exports."""

    def test_wavelength_tracker_exported(self) -> None:
        """WavelengthTracker should be importable from creek.wavelength."""
        from creek.wavelength import WavelengthTracker as TrackerImport

        assert TrackerImport is WavelengthTracker

    def test_phase_transition_exported(self) -> None:
        """PhaseTransition should be importable from creek.wavelength."""
        from creek.wavelength import PhaseTransition as TransitionImport

        assert TransitionImport is PhaseTransition

    def test_wavelength_report_exported(self) -> None:
        """WavelengthReport should be importable from creek.wavelength."""
        from creek.wavelength import WavelengthReport as ReportImport

        assert ReportImport is WavelengthReport
