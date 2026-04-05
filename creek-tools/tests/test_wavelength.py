"""Tests for creek.wavelength — wavelength tracking and phase analysis.

Tests cover:
- WindowSummary model structure and defaults
- PhaseTransition model structure
- DosageTrend model structure and defaults
- WavelengthTracker.summarize_windows with single and multiple windows
- WavelengthTracker.detect_transitions for phase changes
- WavelengthTracker.compute_dosage_trend for rising/falling/stable
- WavelengthTracker.generate_observations producing WavelengthObservation
- Configurable window_days and min_fragments parameters
- Edge cases: empty fragments, all unclassified, single fragment
"""

from datetime import date, datetime, timedelta

from creek.models import (
    Confidence,
    Dosage,
    Fragment,
    FragmentSource,
    Mode,
    Phase,
    SourcePlatform,
    WavelengthClassification,
    WavelengthObservation,
)
from creek.wavelength.tracker import (
    DosageTrend,
    PhaseTransition,
    WavelengthTracker,
    WindowSummary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fragment(
    *,
    created: datetime,
    phase: Phase = Phase.UNCLASSIFIED,
    mode: Mode = Mode.UNCLASSIFIED,
    dosage: Dosage = Dosage.UNCLASSIFIED,
) -> Fragment:
    """Create a fragment with specified wavelength classification."""
    return Fragment(
        title="test fragment",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        wavelength=WavelengthClassification(
            phase=phase,
            mode=mode,
            dosage=dosage,
        ),
    )


def _date_to_dt(d: date) -> datetime:
    """Convert a date to a datetime at midnight."""
    return datetime(d.year, d.month, d.day)


# ---------------------------------------------------------------------------
# WindowSummary model
# ---------------------------------------------------------------------------


class TestWindowSummary:
    """Tests for the WindowSummary model."""

    def test_required_fields(self) -> None:
        """WindowSummary should have start, end, and fragment_count fields."""
        summary = WindowSummary(
            start=date(2025, 1, 1),
            end=date(2025, 1, 7),
            fragment_count=5,
        )
        assert summary.start == date(2025, 1, 1)
        assert summary.end == date(2025, 1, 7)
        assert summary.fragment_count == 5

    def test_defaults(self) -> None:
        """WindowSummary should have sensible defaults."""
        summary = WindowSummary(
            start=date(2025, 1, 1),
            end=date(2025, 1, 7),
        )
        assert summary.dominant_phase == Phase.UNCLASSIFIED
        assert summary.dominant_mode == Mode.UNCLASSIFIED
        assert summary.phase_counts == {}
        assert summary.mode_counts == {}
        assert summary.dosage_counts == {}
        assert summary.fragment_count == 0
        assert summary.toxic_ratio == 0.0


# ---------------------------------------------------------------------------
# PhaseTransition model
# ---------------------------------------------------------------------------


class TestPhaseTransition:
    """Tests for the PhaseTransition model."""

    def test_required_fields(self) -> None:
        """PhaseTransition should capture from, to, and date."""
        transition = PhaseTransition(
            from_phase=Phase.RISING,
            to_phase=Phase.PEAKING,
            transition_date=date(2025, 1, 8),
        )
        assert transition.from_phase == Phase.RISING
        assert transition.to_phase == Phase.PEAKING
        assert transition.transition_date == date(2025, 1, 8)


# ---------------------------------------------------------------------------
# DosageTrend model
# ---------------------------------------------------------------------------


class TestDosageTrend:
    """Tests for the DosageTrend model."""

    def test_defaults(self) -> None:
        """DosageTrend should default to stable with no data."""
        trend = DosageTrend()
        assert trend.window_ratios == []
        assert trend.trend == "stable"
        assert trend.latest_ratio == 0.0

    def test_with_data(self) -> None:
        """DosageTrend should store window_ratios and latest_ratio."""
        trend = DosageTrend(
            window_ratios=[(date(2025, 1, 1), 0.3), (date(2025, 1, 8), 0.5)],
            trend="rising",
            latest_ratio=0.5,
        )
        assert len(trend.window_ratios) == 2
        assert trend.trend == "rising"
        assert trend.latest_ratio == 0.5


# ---------------------------------------------------------------------------
# WavelengthTracker — summarize_windows
# ---------------------------------------------------------------------------


class TestSummarizeWindows:
    """Tests for WavelengthTracker.summarize_windows."""

    def test_empty_fragments_returns_empty(self) -> None:
        """Empty input should produce no summaries."""
        tracker = WavelengthTracker()
        assert tracker.summarize_windows([]) == []

    def test_single_fragment_single_window(self) -> None:
        """A single fragment should produce one window summary."""
        tracker = WavelengthTracker(window_days=7)
        fragments = [
            _make_fragment(
                created=_date_to_dt(date(2025, 3, 1)),
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                dosage=Dosage.MEDICINE,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert len(summaries) == 1
        assert summaries[0].dominant_phase == Phase.RISING
        assert summaries[0].dominant_mode == Mode.EXPRESS
        assert summaries[0].fragment_count == 1

    def test_multiple_fragments_same_window(self) -> None:
        """Fragments within the same window should be grouped together."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(
                created=_date_to_dt(base),
                phase=Phase.RISING,
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=2)),
                phase=Phase.RISING,
                dosage=Dosage.TOXIC,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=5)),
                phase=Phase.PEAKING,
                dosage=Dosage.MEDICINE,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert len(summaries) == 1
        assert summaries[0].fragment_count == 3
        assert summaries[0].dominant_phase == Phase.RISING  # 2 vs 1

    def test_fragments_across_multiple_windows(self) -> None:
        """Fragments spanning multiple weeks should produce multiple summaries."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(
                created=_date_to_dt(base),
                phase=Phase.RISING,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=10)),
                phase=Phase.PEAKING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert len(summaries) == 2
        assert summaries[0].dominant_phase == Phase.RISING
        assert summaries[1].dominant_phase == Phase.PEAKING

    def test_min_fragments_filter(self) -> None:
        """Windows below min_fragments should be excluded."""
        tracker = WavelengthTracker(window_days=7, min_fragments=3)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(created=_date_to_dt(base), phase=Phase.RISING),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=1)),
                phase=Phase.RISING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert len(summaries) == 0  # only 2 fragments, need 3

    def test_custom_window_days(self) -> None:
        """Custom window_days should change grouping boundaries."""
        tracker = WavelengthTracker(window_days=3)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(created=_date_to_dt(base), phase=Phase.RISING),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=4)),
                phase=Phase.PEAKING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        # day 0 -> window 0, day 4 -> window 1 (4 // 3 = 1)
        assert len(summaries) == 2

    def test_toxic_ratio_computed(self) -> None:
        """Toxic ratio should reflect proportion of toxic dosages."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(
                created=_date_to_dt(base),
                dosage=Dosage.MEDICINE,
                phase=Phase.RISING,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=1)),
                dosage=Dosage.TOXIC,
                phase=Phase.RISING,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=2)),
                dosage=Dosage.MEDICINE,
                phase=Phase.RISING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        # 1 toxic out of 3 classified = ~0.333
        assert 0.3 <= summaries[0].toxic_ratio <= 0.34

    def test_unclassified_dosage_excluded_from_ratio(self) -> None:
        """Unclassified dosages should not affect the toxic ratio denominator."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(
                created=_date_to_dt(base),
                dosage=Dosage.TOXIC,
                phase=Phase.RISING,
            ),
            _make_fragment(
                created=_date_to_dt(base + timedelta(days=1)),
                dosage=Dosage.UNCLASSIFIED,
                phase=Phase.RISING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        # 1 toxic out of 1 classified = 1.0
        assert summaries[0].toxic_ratio == 1.0

    def test_all_unclassified_phases_returns_unclassified_dominant(self) -> None:
        """When all fragments are unclassified, dominant phase is unclassified."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 3, 1)
        fragments = [
            _make_fragment(created=_date_to_dt(base)),
            _make_fragment(created=_date_to_dt(base + timedelta(days=1))),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert summaries[0].dominant_phase == Phase.UNCLASSIFIED

    def test_window_dates_correct(self) -> None:
        """Window start and end dates should be computed correctly."""
        tracker = WavelengthTracker(window_days=7)
        fragments = [
            _make_fragment(
                created=_date_to_dt(date(2025, 3, 1)),
                phase=Phase.RISING,
            ),
        ]
        summaries = tracker.summarize_windows(fragments)
        assert summaries[0].start == date(2025, 3, 1)
        assert summaries[0].end == date(2025, 3, 7)


# ---------------------------------------------------------------------------
# WavelengthTracker — detect_transitions
# ---------------------------------------------------------------------------


class TestDetectTransitions:
    """Tests for WavelengthTracker.detect_transitions."""

    def test_no_transitions_same_phase(self) -> None:
        """Consecutive windows with the same phase produce no transitions."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                fragment_count=5,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                dominant_phase=Phase.RISING,
                fragment_count=3,
            ),
        ]
        transitions = tracker.detect_transitions(summaries)
        assert len(transitions) == 0

    def test_single_transition(self) -> None:
        """A phase change between windows should produce one transition."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                fragment_count=5,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                dominant_phase=Phase.PEAKING,
                fragment_count=3,
            ),
        ]
        transitions = tracker.detect_transitions(summaries)
        assert len(transitions) == 1
        assert transitions[0].from_phase == Phase.RISING
        assert transitions[0].to_phase == Phase.PEAKING
        assert transitions[0].transition_date == date(2025, 1, 8)

    def test_multiple_transitions(self) -> None:
        """Multiple phase changes should produce multiple transitions."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                fragment_count=3,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                dominant_phase=Phase.PEAKING,
                fragment_count=3,
            ),
            WindowSummary(
                start=date(2025, 1, 15),
                end=date(2025, 1, 21),
                dominant_phase=Phase.WITHDRAWAL,
                fragment_count=3,
            ),
        ]
        transitions = tracker.detect_transitions(summaries)
        assert len(transitions) == 2

    def test_unclassified_skipped_in_transitions(self) -> None:
        """Unclassified windows should be ignored in transition detection."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                fragment_count=3,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                dominant_phase=Phase.UNCLASSIFIED,
                fragment_count=1,
            ),
            WindowSummary(
                start=date(2025, 1, 15),
                end=date(2025, 1, 21),
                dominant_phase=Phase.PEAKING,
                fragment_count=3,
            ),
        ]
        transitions = tracker.detect_transitions(summaries)
        assert len(transitions) == 1
        assert transitions[0].from_phase == Phase.RISING
        assert transitions[0].to_phase == Phase.PEAKING

    def test_empty_summaries_no_transitions(self) -> None:
        """Empty summaries should produce no transitions."""
        tracker = WavelengthTracker()
        assert tracker.detect_transitions([]) == []


# ---------------------------------------------------------------------------
# WavelengthTracker — compute_dosage_trend
# ---------------------------------------------------------------------------


class TestComputeDosageTrend:
    """Tests for WavelengthTracker.compute_dosage_trend."""

    def test_empty_summaries_stable(self) -> None:
        """No summaries should produce a stable trend with no data."""
        tracker = WavelengthTracker()
        trend = tracker.compute_dosage_trend([])
        assert trend.trend == "stable"
        assert trend.window_ratios == []
        assert trend.latest_ratio == 0.0

    def test_rising_trend(self) -> None:
        """Increasing toxic ratio across windows should be detected as rising."""
        tracker = WavelengthTracker()
        base = date(2025, 1, 1)
        summaries = [
            WindowSummary(
                start=base + timedelta(days=d * 7),
                end=base + timedelta(days=d * 7 + 6),
                fragment_count=5,
                toxic_ratio=ratio,
            )
            for d, ratio in enumerate([0.1, 0.15, 0.2, 0.3, 0.4, 0.5])
        ]
        trend = tracker.compute_dosage_trend(summaries)
        assert trend.trend == "rising"
        assert trend.latest_ratio == 0.5

    def test_falling_trend(self) -> None:
        """Decreasing toxic ratio across windows should be detected as falling."""
        tracker = WavelengthTracker()
        base = date(2025, 1, 1)
        summaries = [
            WindowSummary(
                start=base + timedelta(days=d * 7),
                end=base + timedelta(days=d * 7 + 6),
                fragment_count=5,
                toxic_ratio=ratio,
            )
            for d, ratio in enumerate([0.6, 0.5, 0.4, 0.3, 0.2, 0.1])
        ]
        trend = tracker.compute_dosage_trend(summaries)
        assert trend.trend == "falling"
        assert trend.latest_ratio == 0.1

    def test_stable_trend(self) -> None:
        """Consistent toxic ratio should be detected as stable."""
        tracker = WavelengthTracker()
        base = date(2025, 1, 1)
        summaries = [
            WindowSummary(
                start=base + timedelta(days=d * 7),
                end=base + timedelta(days=d * 7 + 6),
                fragment_count=5,
                toxic_ratio=0.3,
            )
            for d in range(6)
        ]
        trend = tracker.compute_dosage_trend(summaries)
        assert trend.trend == "stable"

    def test_fewer_than_three_windows_stable(self) -> None:
        """With fewer than 3 windows, trend should default to stable."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                fragment_count=5,
                toxic_ratio=0.8,
            ),
        ]
        trend = tracker.compute_dosage_trend(summaries)
        assert trend.trend == "stable"

    def test_window_ratios_preserved(self) -> None:
        """Window ratios should be preserved in order."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                fragment_count=3,
                toxic_ratio=0.2,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                fragment_count=3,
                toxic_ratio=0.4,
            ),
        ]
        trend = tracker.compute_dosage_trend(summaries)
        assert len(trend.window_ratios) == 2
        assert trend.window_ratios[0] == (date(2025, 1, 1), 0.2)
        assert trend.window_ratios[1] == (date(2025, 1, 8), 0.4)


# ---------------------------------------------------------------------------
# WavelengthTracker — generate_observations
# ---------------------------------------------------------------------------


class TestGenerateObservations:
    """Tests for WavelengthTracker.generate_observations."""

    def test_generates_observation_per_classified_window(self) -> None:
        """Each classified window should produce one WavelengthObservation."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                dominant_mode=Mode.EXPRESS,
                dosage_counts={"medicine": 3, "toxic": 1},
                fragment_count=4,
                toxic_ratio=0.25,
            ),
            WindowSummary(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                dominant_phase=Phase.PEAKING,
                dominant_mode=Mode.COLLABORATE,
                dosage_counts={"medicine": 5},
                fragment_count=5,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert len(observations) == 2
        assert all(isinstance(o, WavelengthObservation) for o in observations)
        assert observations[0].phase == Phase.RISING
        assert observations[1].phase == Phase.PEAKING

    def test_skips_unclassified_windows(self) -> None:
        """Windows with unclassified dominant phase should not produce observations."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.UNCLASSIFIED,
                fragment_count=2,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert len(observations) == 0

    def test_observation_has_wave_id(self) -> None:
        """Generated observations should have wave- prefixed IDs."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                dosage_counts={"medicine": 2},
                fragment_count=2,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert observations[0].id.startswith("wave-")

    def test_observation_notes_include_window_info(self) -> None:
        """Observation notes should describe the window."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                dosage_counts={"medicine": 3},
                fragment_count=3,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert "2025-01-01" in observations[0].notes
        assert "3 fragments" in observations[0].notes

    def test_observation_confidence_scales_with_count(self) -> None:
        """Higher fragment counts should produce higher confidence."""
        tracker = WavelengthTracker()
        small_summary = WindowSummary(
            start=date(2025, 1, 1),
            end=date(2025, 1, 7),
            dominant_phase=Phase.RISING,
            phase_counts={"rising": 1},
            dosage_counts={"medicine": 1},
            fragment_count=1,
            toxic_ratio=0.0,
        )
        large_summary = WindowSummary(
            start=date(2025, 1, 8),
            end=date(2025, 1, 14),
            dominant_phase=Phase.RISING,
            phase_counts={"rising": 12},
            dosage_counts={"medicine": 12},
            fragment_count=12,
            toxic_ratio=0.0,
        )
        small_obs = tracker.generate_observations([small_summary])
        large_obs = tracker.generate_observations([large_summary])

        # Map confidence to numeric for comparison
        conf_order = [
            Confidence.MUSING,
            Confidence.EXPLORING,
            Confidence.FORMING,
            Confidence.SETTLED,
            Confidence.CONVICTION,
        ]
        assert conf_order.index(large_obs[0].confidence) >= conf_order.index(
            small_obs[0].confidence
        )

    def test_empty_summaries_no_observations(self) -> None:
        """Empty summaries should produce no observations."""
        tracker = WavelengthTracker()
        assert tracker.generate_observations([]) == []

    def test_dominant_dosage_in_observation(self) -> None:
        """Observation dosage should reflect the dominant dosage in the window."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.DIMINISHING,
                dosage_counts={"toxic": 4, "medicine": 1},
                fragment_count=5,
                toxic_ratio=0.8,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert observations[0].dosage == Dosage.TOXIC

    def test_all_unclassified_dosage_returns_unclassified(self) -> None:
        """When all dosages are unclassified, observation dosage is unclassified."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                dosage_counts={"unclassified": 3},
                phase_counts={"rising": 3},
                fragment_count=3,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert observations[0].dosage == Dosage.UNCLASSIFIED

    def test_confidence_settled_for_moderate_count(self) -> None:
        """5+ fragments with 60%+ agreement should produce SETTLED confidence."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                phase_counts={"rising": 4, "peaking": 2},
                dosage_counts={"medicine": 6},
                fragment_count=6,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert observations[0].confidence == Confidence.SETTLED

    def test_confidence_exploring_for_two_fragments(self) -> None:
        """2 fragments should produce EXPLORING confidence."""
        tracker = WavelengthTracker()
        summaries = [
            WindowSummary(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                dominant_phase=Phase.RISING,
                phase_counts={"rising": 1, "peaking": 1},
                dosage_counts={"medicine": 2},
                fragment_count=2,
                toxic_ratio=0.0,
            ),
        ]
        observations = tracker.generate_observations(summaries)
        assert observations[0].confidence == Confidence.EXPLORING


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------


class TestWavelengthTrackerIntegration:
    """Integration tests exercising the full tracker pipeline."""

    def test_full_cycle_detection(self) -> None:
        """Tracker should detect a full wavelength cycle across windows."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 1, 1)
        phases = [
            Phase.RISING,
            Phase.PEAKING,
            Phase.WITHDRAWAL,
            Phase.DIMINISHING,
            Phase.BOTTOMING_OUT,
            Phase.RESTORATION,
        ]
        fragments = []
        for i, phase in enumerate(phases):
            # 3 fragments per phase window
            for j in range(3):
                fragments.append(
                    _make_fragment(
                        created=_date_to_dt(
                            base + timedelta(days=i * 7 + j),
                        ),
                        phase=phase,
                        mode=Mode.INHABIT,
                        dosage=Dosage.MEDICINE,
                    )
                )

        summaries = tracker.summarize_windows(fragments)
        transitions = tracker.detect_transitions(summaries)
        observations = tracker.generate_observations(summaries)

        assert len(summaries) == 6
        assert len(transitions) == 5  # 6 phases = 5 transitions
        assert len(observations) == 6
        assert transitions[0].from_phase == Phase.RISING
        assert transitions[0].to_phase == Phase.PEAKING
        assert transitions[-1].to_phase == Phase.RESTORATION

    def test_dosage_trend_across_cycle(self) -> None:
        """Dosage trend should detect escalating toxic patterns."""
        tracker = WavelengthTracker(window_days=7)
        base = date(2025, 1, 1)
        fragments = []

        # First 3 windows: medicine-dominant
        for week in range(3):
            for day in range(3):
                fragments.append(
                    _make_fragment(
                        created=_date_to_dt(
                            base + timedelta(days=week * 7 + day),
                        ),
                        phase=Phase.RISING,
                        dosage=Dosage.MEDICINE,
                    )
                )

        # Last 3 windows: toxic-dominant
        for week in range(3, 6):
            for day in range(3):
                fragments.append(
                    _make_fragment(
                        created=_date_to_dt(
                            base + timedelta(days=week * 7 + day),
                        ),
                        phase=Phase.DIMINISHING,
                        dosage=Dosage.TOXIC,
                    )
                )

        summaries = tracker.summarize_windows(fragments)
        trend = tracker.compute_dosage_trend(summaries)

        assert trend.trend == "rising"
        assert trend.latest_ratio == 1.0
