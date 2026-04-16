"""Tests for creek.generate.wavelength — wavelength tracking.

Covers WavelengthTracker's four public methods: analyze_period (snapshot
aggregation), detect_transitions (phase-change detection), track_dosage_trends
(rolling medicine/toxic ratios per frequency), and generate_report (writing a
wavelength report into the vault at ``05-Wavelength/Phase-Maps/``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.wavelength import (
    DEFAULT_ROLLING_WEEKS,
    DEFAULT_TOXIC_CONSECUTIVE_WEEKS,
    DEFAULT_TOXIC_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    DOMAIN_MAPPINGS,
    PHASE_DESCRIPTIONS,
    DosageTrend,
    PhaseTransition,
    WavelengthSnapshot,
    WavelengthTracker,
)
from creek.models import (
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


def _make_fragment(
    *,
    frag_id: str,
    title: str = "test fragment",
    created: datetime,
    phase: Phase = Phase.UNCLASSIFIED,
    mode: Mode = Mode.UNCLASSIFIED,
    dosage: Dosage = Dosage.UNCLASSIFIED,
    emotional_texture: list[str] | None = None,
    frequency: Frequency = Frequency.UNCLASSIFIED,
) -> Fragment:
    """Construct a test Fragment with the given wavelength/frequency fields."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(
            phase=phase,
            mode=mode,
            dosage=dosage,
        ),
        emotional_texture=emotional_texture or [],
    )


@pytest.fixture()
def tracker() -> WavelengthTracker:
    """Return a default WavelengthTracker."""
    return WavelengthTracker()


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Return a vault root with the expected Phase-Maps folder."""
    (tmp_path / "05-Wavelength" / "Phase-Maps").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---- Module Constants ----


class TestConstants:
    """Tests for module-level default constants."""

    def test_window_days_is_seven(self) -> None:
        """Default window is 7 days per Section 7.5 technical notes."""
        assert DEFAULT_WINDOW_DAYS == 7

    def test_rolling_weeks_is_four(self) -> None:
        """Default rolling average for dosage is 4 weeks."""
        assert DEFAULT_ROLLING_WEEKS == 4

    def test_toxic_threshold_is_point_six(self) -> None:
        """Frequencies above 60% toxic flag for potential overdose."""
        assert pytest.approx(0.6) == DEFAULT_TOXIC_THRESHOLD

    def test_consecutive_weeks_is_three(self) -> None:
        """Flag triggers after 3+ consecutive weeks over threshold."""
        assert DEFAULT_TOXIC_CONSECUTIVE_WEEKS == 3


# ---- Init ----


class TestTrackerInit:
    """Tests for WavelengthTracker.__init__."""

    def test_defaults(self) -> None:
        """Default constructor uses module-level thresholds."""
        trk = WavelengthTracker()
        assert trk.window_days == DEFAULT_WINDOW_DAYS
        assert trk.rolling_weeks == DEFAULT_ROLLING_WEEKS
        assert trk.toxic_threshold == pytest.approx(DEFAULT_TOXIC_THRESHOLD)
        assert trk.consecutive_weeks == DEFAULT_TOXIC_CONSECUTIVE_WEEKS

    def test_accepts_overrides(self) -> None:
        """Constructor accepts override parameters."""
        trk = WavelengthTracker(
            window_days=14,
            rolling_weeks=6,
            toxic_threshold=0.75,
            consecutive_weeks=2,
        )
        assert trk.window_days == 14
        assert trk.rolling_weeks == 6
        assert trk.toxic_threshold == pytest.approx(0.75)
        assert trk.consecutive_weeks == 2

    def test_rejects_zero_window_days(self) -> None:
        """Zero-length windows would infinite-loop; the constructor rejects them."""
        with pytest.raises(ValueError, match="window_days"):
            WavelengthTracker(window_days=0)

    def test_rejects_negative_window_days(self) -> None:
        """Negative window_days is also rejected."""
        with pytest.raises(ValueError, match="window_days"):
            WavelengthTracker(window_days=-3)

    def test_rejects_zero_rolling_weeks(self) -> None:
        """Zero rolling weeks would produce an empty rolling average."""
        with pytest.raises(ValueError, match="rolling_weeks"):
            WavelengthTracker(rolling_weeks=0)

    def test_rejects_negative_rolling_weeks(self) -> None:
        """Negative rolling_weeks is rejected."""
        with pytest.raises(ValueError, match="rolling_weeks"):
            WavelengthTracker(rolling_weeks=-2)

    def test_rejects_zero_consecutive_weeks(self) -> None:
        """consecutive_weeks below 1 would flag every frequency vacuously."""
        with pytest.raises(ValueError, match="consecutive_weeks"):
            WavelengthTracker(consecutive_weeks=0)

    def test_rejects_toxic_threshold_above_one(self) -> None:
        """A threshold above 1.0 can never be exceeded and would never flag."""
        with pytest.raises(ValueError, match="toxic_threshold"):
            WavelengthTracker(toxic_threshold=1.5)

    def test_rejects_toxic_threshold_at_zero(self) -> None:
        """A threshold of 0.0 flags every non-zero toxic ratio and is vacuous."""
        with pytest.raises(ValueError, match="toxic_threshold"):
            WavelengthTracker(toxic_threshold=0.0)

    def test_rejects_negative_toxic_threshold(self) -> None:
        """Negative thresholds are rejected."""
        with pytest.raises(ValueError, match="toxic_threshold"):
            WavelengthTracker(toxic_threshold=-0.1)

    def test_accepts_toxic_threshold_of_one(self) -> None:
        """The upper bound 1.0 is inclusive — flagging requires 100% toxic."""
        trk = WavelengthTracker(toxic_threshold=1.0)
        assert trk.toxic_threshold == pytest.approx(1.0)


# ---- analyze_period ----


class TestAnalyzePeriod:
    """Tests for WavelengthTracker.analyze_period."""

    def test_empty_fragments_returns_unclassified_snapshot(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """With no fragments, the snapshot reports unclassified dominants."""
        snap = tracker.analyze_period([], date(2025, 1, 1), date(2025, 1, 7))
        assert snap.dominant_phase == Phase.UNCLASSIFIED.value
        assert snap.dominant_mode == Mode.UNCLASSIFIED.value
        assert snap.fragment_count == 0

    def test_snapshot_records_period_bounds(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Start and end dates flow through to the snapshot."""
        snap = tracker.analyze_period([], date(2025, 2, 1), date(2025, 2, 7))
        assert snap.start_date == date(2025, 2, 1)
        assert snap.end_date == date(2025, 2, 7)

    def test_filters_fragments_outside_window(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Fragments created outside the [start, end] window are ignored."""
        fragments = [
            _make_fragment(
                frag_id="frag-1",
                created=datetime(2025, 1, 15, 10, 0, 0),
                phase=Phase.PEAKING,
            ),
            _make_fragment(
                frag_id="frag-2",
                created=datetime(2025, 1, 20, 10, 0, 0),
                phase=Phase.WITHDRAWAL,
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.fragment_count == 0

    def test_dominant_phase_is_most_common(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Dominant phase equals the most frequent phase in the window."""
        fragments = [
            _make_fragment(
                frag_id=f"frag-{i}",
                created=datetime(2025, 1, 3, 10, i, 0),
                phase=Phase.PEAKING,
            )
            for i in range(3)
        ]
        fragments.append(
            _make_fragment(
                frag_id="frag-other",
                created=datetime(2025, 1, 4, 10, 0, 0),
                phase=Phase.WITHDRAWAL,
            ),
        )
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.dominant_phase == Phase.PEAKING.value
        assert snap.fragment_count == 4

    def test_dominant_mode_is_most_common(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Dominant mode equals the most frequent mode in the window."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 2),
                mode=Mode.INHABIT,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 3),
                mode=Mode.INHABIT,
            ),
            _make_fragment(
                frag_id="c",
                created=datetime(2025, 1, 4),
                mode=Mode.EXPRESS,
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.dominant_mode == Mode.INHABIT.value

    def test_unclassified_values_are_ignored_for_dominant(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Unclassified phases don't count toward the dominant phase."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 2),
                phase=Phase.UNCLASSIFIED,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 3),
                phase=Phase.UNCLASSIFIED,
            ),
            _make_fragment(
                frag_id="c",
                created=datetime(2025, 1, 4),
                phase=Phase.RISING,
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.dominant_phase == Phase.RISING.value

    def test_dosage_percentages_sum_correctly(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Medicine and toxic percentages reflect fragment counts."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 2),
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 3),
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="c",
                created=datetime(2025, 1, 4),
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="d",
                created=datetime(2025, 1, 5),
                dosage=Dosage.TOXIC,
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.medicine_percent == pytest.approx(0.75)
        assert snap.toxic_percent == pytest.approx(0.25)

    def test_emotional_texture_cloud_counts_tags(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Emotional texture cloud aggregates and counts tag occurrences."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 2),
                emotional_texture=["grief", "tender"],
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 3),
                emotional_texture=["grief"],
            ),
            _make_fragment(
                frag_id="c",
                created=datetime(2025, 1, 4),
                emotional_texture=["joy"],
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        cloud = dict(snap.emotional_texture_cloud)
        assert cloud["grief"] == 2
        assert cloud["tender"] == 1
        assert cloud["joy"] == 1

    def test_emotional_texture_cloud_ordered_by_count(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Cloud entries are ordered most-common first."""
        fragments = [
            _make_fragment(
                frag_id=f"frag-{i}",
                created=datetime(2025, 1, 3, 10, i, 0),
                emotional_texture=["grief"],
            )
            for i in range(3)
        ]
        fragments.append(
            _make_fragment(
                frag_id="frag-joy",
                created=datetime(2025, 1, 4),
                emotional_texture=["joy"],
            ),
        )
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.emotional_texture_cloud[0] == ("grief", 3)

    def test_confidence_is_one_when_fully_consistent(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Confidence is 1.0 when all fragments share the same phase."""
        fragments = [
            _make_fragment(
                frag_id=f"frag-{i}",
                created=datetime(2025, 1, 3, 10, i, 0),
                phase=Phase.RISING,
            )
            for i in range(5)
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.confidence == pytest.approx(1.0)

    def test_confidence_reflects_diversity(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Confidence is lower when phases are split."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 2),
                phase=Phase.RISING,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 3),
                phase=Phase.WITHDRAWAL,
            ),
        ]
        snap = tracker.analyze_period(
            fragments,
            date(2025, 1, 1),
            date(2025, 1, 7),
        )
        assert snap.confidence == pytest.approx(0.5)


# ---- detect_transitions ----


def _snapshot(
    *,
    start: date,
    end: date,
    phase: str,
    supporting_ids: list[str] | None = None,
) -> WavelengthSnapshot:
    """Construct a WavelengthSnapshot with minimal fields for transition tests."""
    return WavelengthSnapshot(
        start_date=start,
        end_date=end,
        dominant_phase=phase,
        dominant_mode=Mode.UNCLASSIFIED.value,
        medicine_percent=0.0,
        toxic_percent=0.0,
        emotional_texture_cloud=[],
        confidence=1.0,
        fragment_count=len(supporting_ids or []),
        supporting_fragment_ids=supporting_ids or [],
    )


class TestDetectTransitions:
    """Tests for WavelengthTracker.detect_transitions."""

    def test_empty_snapshots_returns_empty(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """No snapshots means no transitions."""
        assert tracker.detect_transitions([]) == []

    def test_single_snapshot_returns_empty(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """A single snapshot cannot produce a transition."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.PEAKING.value,
            ),
        ]
        assert tracker.detect_transitions(snapshots) == []

    def test_no_change_yields_no_transition(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Adjacent snapshots with the same phase don't transition."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.PEAKING.value,
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.PEAKING.value,
            ),
        ]
        assert tracker.detect_transitions(snapshots) == []

    def test_phase_change_detected(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """A phase change between adjacent snapshots yields one transition."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.PEAKING.value,
                supporting_ids=["frag-a"],
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.WITHDRAWAL.value,
                supporting_ids=["frag-b"],
            ),
        ]
        transitions = tracker.detect_transitions(snapshots)
        assert len(transitions) == 1
        assert transitions[0].from_phase == Phase.PEAKING.value
        assert transitions[0].to_phase == Phase.WITHDRAWAL.value

    def test_transition_records_dates_from_both_snapshots(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Transition captures the from-window end and to-window start."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.PEAKING.value,
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.WITHDRAWAL.value,
            ),
        ]
        trans = tracker.detect_transitions(snapshots)[0]
        assert trans.from_date == date(2025, 1, 7)
        assert trans.to_date == date(2025, 1, 8)

    def test_transition_collects_supporting_fragments(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Transition aggregates supporting fragment IDs from both windows."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.PEAKING.value,
                supporting_ids=["frag-a"],
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.WITHDRAWAL.value,
                supporting_ids=["frag-b", "frag-c"],
            ),
        ]
        trans = tracker.detect_transitions(snapshots)[0]
        assert "frag-a" in trans.supporting_fragment_ids
        assert "frag-b" in trans.supporting_fragment_ids
        assert "frag-c" in trans.supporting_fragment_ids

    def test_multiple_transitions_detected(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Each adjacent-pair phase change yields its own transition."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.RISING.value,
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.PEAKING.value,
            ),
            _snapshot(
                start=date(2025, 1, 15),
                end=date(2025, 1, 21),
                phase=Phase.WITHDRAWAL.value,
            ),
        ]
        transitions = tracker.detect_transitions(snapshots)
        assert len(transitions) == 2
        assert transitions[0].to_phase == Phase.PEAKING.value
        assert transitions[1].from_phase == Phase.PEAKING.value

    def test_unclassified_snapshots_do_not_transition(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Transitions to or from UNCLASSIFIED are skipped."""
        snapshots = [
            _snapshot(
                start=date(2025, 1, 1),
                end=date(2025, 1, 7),
                phase=Phase.UNCLASSIFIED.value,
            ),
            _snapshot(
                start=date(2025, 1, 8),
                end=date(2025, 1, 14),
                phase=Phase.PEAKING.value,
            ),
        ]
        assert tracker.detect_transitions(snapshots) == []


# ---- track_dosage_trends ----


class TestTrackDosageTrends:
    """Tests for WavelengthTracker.track_dosage_trends."""

    def test_empty_fragments_returns_empty_trend(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """No fragments means no frequency trends and no flags."""
        trend = tracker.track_dosage_trends([])
        assert isinstance(trend, DosageTrend)
        assert trend.frequency_trends == {}
        assert trend.flagged_frequencies == []

    def test_tracks_per_frequency_series(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Each frequency with classified fragments gets its own rolling series."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 1),
                frequency=Frequency.F1,
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 2),
                frequency=Frequency.F2,
                dosage=Dosage.TOXIC,
            ),
        ]
        trend = tracker.track_dosage_trends(fragments)
        assert Frequency.F1.value in trend.frequency_trends
        assert Frequency.F2.value in trend.frequency_trends

    def test_flags_sustained_high_toxic(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """A frequency over 60% toxic for 3+ weeks is flagged."""
        fragments: list[Fragment] = []
        # 5 weeks where F1 is 100% toxic.
        for week in range(5):
            fragments.append(
                _make_fragment(
                    frag_id=f"frag-{week}",
                    created=datetime(2025, 1, 1 + week * 7),
                    frequency=Frequency.F1,
                    dosage=Dosage.TOXIC,
                ),
            )
        trend = tracker.track_dosage_trends(fragments)
        assert Frequency.F1.value in trend.flagged_frequencies

    def test_does_not_flag_brief_toxic_burst(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """A single toxic week does not trigger the flag."""
        fragments = [
            _make_fragment(
                frag_id="toxic",
                created=datetime(2025, 1, 1),
                frequency=Frequency.F1,
                dosage=Dosage.TOXIC,
            ),
            _make_fragment(
                frag_id="med-1",
                created=datetime(2025, 1, 8),
                frequency=Frequency.F1,
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="med-2",
                created=datetime(2025, 1, 15),
                frequency=Frequency.F1,
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="med-3",
                created=datetime(2025, 1, 22),
                frequency=Frequency.F1,
                dosage=Dosage.MEDICINE,
            ),
        ]
        trend = tracker.track_dosage_trends(fragments)
        assert Frequency.F1.value not in trend.flagged_frequencies

    def test_ignores_unclassified_frequency(
        self,
        tracker: WavelengthTracker,
    ) -> None:
        """Fragments without a classified frequency do not create a series."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 1),
                frequency=Frequency.UNCLASSIFIED,
                dosage=Dosage.TOXIC,
            ),
        ]
        trend = tracker.track_dosage_trends(fragments)
        assert trend.frequency_trends == {}

    def test_ratio_exactly_at_threshold_does_not_flag(self) -> None:
        """``_is_trending_toxic`` uses strict ``>``; exact threshold is safe."""
        trk = WavelengthTracker(
            toxic_threshold=0.6,
            consecutive_weeks=2,
            rolling_weeks=1,
        )
        fragments: list[Fragment] = []
        # Two consecutive weeks where exactly 60% of fragments are toxic.
        # Strict > 0.6 means the flag should NOT fire.
        for week in range(2):
            base_day = 1 + week * 7
            for offset, dosage in enumerate(
                [
                    Dosage.TOXIC,
                    Dosage.TOXIC,
                    Dosage.TOXIC,
                    Dosage.MEDICINE,
                    Dosage.MEDICINE,
                ],
            ):
                fragments.append(
                    _make_fragment(
                        frag_id=f"w{week}-{offset}",
                        created=datetime(2025, 1, base_day, 10, offset, 0),
                        frequency=Frequency.F1,
                        dosage=dosage,
                    ),
                )
        trend = trk.track_dosage_trends(fragments)
        assert Frequency.F1.value not in trend.flagged_frequencies

    def test_rolling_window_larger_than_series(self) -> None:
        """Rolling average handles the case where the window exceeds data points."""
        trk = WavelengthTracker(rolling_weeks=10)
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 6),
                frequency=Frequency.F1,
                dosage=Dosage.TOXIC,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 13),
                frequency=Frequency.F1,
                dosage=Dosage.MEDICINE,
            ),
        ]
        trend = trk.track_dosage_trends(fragments)
        series = trend.frequency_trends[Frequency.F1.value]
        # Week 1 (all toxic) -> 1.0. Week 2 average over both -> 0.5.
        assert series[0][1] == pytest.approx(1.0)
        assert series[1][1] == pytest.approx(0.5)


# ---- generate_report ----


@pytest.fixture()
def sample_fragments() -> list[Fragment]:
    """Fragments spanning five weeks with a visible RISING -> PEAKING shift."""
    fragments: list[Fragment] = []
    for day in range(7):
        fragments.append(
            _make_fragment(
                frag_id=f"rise-{day}",
                created=datetime(2025, 1, 1 + day, 12, 0, 0),
                phase=Phase.RISING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                frequency=Frequency.F1,
                emotional_texture=["eager"],
            ),
        )
    for day in range(7):
        fragments.append(
            _make_fragment(
                frag_id=f"peak-{day}",
                created=datetime(2025, 1, 8 + day, 12, 0, 0),
                phase=Phase.PEAKING,
                mode=Mode.EXPRESS,
                dosage=Dosage.MEDICINE,
                frequency=Frequency.F2,
                emotional_texture=["radiant"],
            ),
        )
    return fragments


class TestGenerateReport:
    """Tests for WavelengthTracker.generate_report."""

    def test_writes_file_to_phase_maps(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Report lives under ``05-Wavelength/Phase-Maps/``."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        assert path.exists()
        rel = path.relative_to(vault_path)
        assert rel.parts[0] == "05-Wavelength"
        assert rel.parts[1] == "Phase-Maps"

    def test_creates_target_dir_if_missing(
        self,
        tracker: WavelengthTracker,
        tmp_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Target directory is created when it does not exist."""
        path = tracker.generate_report(tmp_path, "weekly", sample_fragments)
        assert path.exists()

    def test_report_has_wavelength_type(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Frontmatter includes ``type: wavelength-report``."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        post = frontmatter.load(str(path))
        assert post["type"] == "wavelength-report"

    def test_report_has_period(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Frontmatter records the report period (weekly or monthly)."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        post = frontmatter.load(str(path))
        assert post["period"] == "weekly"

    def test_report_rejects_unknown_period(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Only ``weekly`` and ``monthly`` are accepted report periods."""
        with pytest.raises(ValueError, match="period"):
            tracker.generate_report(vault_path, "daily", sample_fragments)

    def test_report_body_has_required_sections(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Report body has all six rendered sections."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        content = path.read_text(encoding="utf-8")
        assert "Current Phase" in content
        assert "Phase History" in content
        assert "Dosage Trends" in content
        assert "Transition Log" in content
        assert "Emotional Texture Cloud" in content
        assert "Fragments by Phase" in content

    def test_report_is_descriptive_not_prescriptive(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Report must not contain prescriptive words like 'should'/'must'."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        content = path.read_text(encoding="utf-8").lower()
        assert "you should" not in content
        assert "you must" not in content
        assert "you need to" not in content

    def test_report_includes_dataview_query(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Report includes at least one Dataview query block."""
        path = tracker.generate_report(vault_path, "weekly", sample_fragments)
        content = path.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_empty_fragments_still_generates_report(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Report generation is robust to an empty fragment list."""
        path = tracker.generate_report(vault_path, "monthly", [])
        assert path.exists()

    def test_report_flagged_frequencies_are_listed(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Flagged frequencies appear in the Dosage Trends section."""
        fragments = [
            _make_fragment(
                frag_id=f"frag-{week}",
                created=datetime(2025, 1, 1 + week * 7),
                frequency=Frequency.F3,
                dosage=Dosage.TOXIC,
                phase=Phase.DIMINISHING,
            )
            for week in range(5)
        ]
        path = tracker.generate_report(vault_path, "weekly", fragments)
        content = path.read_text(encoding="utf-8")
        assert "F3" in content

    def test_report_monthly_filename_suffix(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Monthly reports end with ``-monthly.md``."""
        path = tracker.generate_report(vault_path, "monthly", sample_fragments)
        assert path.name.endswith("-monthly.md")

    def test_report_date_is_deterministic(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """``report_date`` pins the filename and the ``generated_on`` field."""
        path = tracker.generate_report(
            vault_path,
            "weekly",
            sample_fragments,
            report_date=date(2025, 6, 1),
        )
        assert path.name == "2025-06-01-weekly.md"
        post = frontmatter.load(str(path))
        assert post["generated_on"] == "2025-06-01"

    def test_report_overwrites_same_day_file(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        sample_fragments: list[Fragment],
    ) -> None:
        """Regenerating on the same date overwrites the prior file."""
        stamp = date(2025, 6, 1)
        first = tracker.generate_report(
            vault_path,
            "weekly",
            sample_fragments,
            report_date=stamp,
        )
        second = tracker.generate_report(
            vault_path,
            "weekly",
            [],  # empty fragments -> different content
            report_date=stamp,
        )
        assert first == second
        # Second run's content differs from first because input changed.
        content = second.read_text(encoding="utf-8")
        assert "No fragments available for this period" in content


# ---- PhaseTransition / DosageTrend dataclasses ----


class TestDataclasses:
    """Surface tests for PhaseTransition / DosageTrend defaults."""

    def test_phase_transition_records_core_fields(self) -> None:
        """PhaseTransition stores from/to phases, dates, and fragment IDs."""
        trans = PhaseTransition(
            from_phase=Phase.PEAKING.value,
            to_phase=Phase.WITHDRAWAL.value,
            from_date=date(2025, 1, 7),
            to_date=date(2025, 1, 8),
            supporting_fragment_ids=["frag-a"],
        )
        assert trans.from_phase == Phase.PEAKING.value
        assert trans.supporting_fragment_ids == ["frag-a"]

    def test_dosage_trend_default_empty_lists(self) -> None:
        """DosageTrend initialises with empty structures."""
        trend = DosageTrend()
        assert trend.frequency_trends == {}
        assert trend.flagged_frequencies == []


# ---- Domain Mappings (ontology Section 7.1) ----


class TestDomainMappings:
    """Tests for the static DOMAIN_MAPPINGS lookup table."""

    def test_all_six_phases_have_mappings(self) -> None:
        """Every classified Archetypal Wavelength phase has a domain mapping."""
        expected_phases = {
            Phase.RISING.value,
            Phase.PEAKING.value,
            Phase.WITHDRAWAL.value,
            Phase.DIMINISHING.value,
            Phase.BOTTOMING_OUT.value,
            Phase.RESTORATION.value,
        }
        assert expected_phases <= set(DOMAIN_MAPPINGS)

    def test_mapping_has_required_domains(self) -> None:
        """Each mapping covers the eight domains Issue #43 requires."""
        required = {
            "season",
            "mood",
            "spaciousness",
            "relation_to_others",
            "relation_to_self",
            "buddhist_attachment",
            "meditation",
            "breath",
        }
        for phase, mapping in DOMAIN_MAPPINGS.items():
            assert required <= set(mapping), (
                f"phase {phase!r} missing domains: {required - set(mapping)}"
            )

    def test_rising_season_is_summer(self) -> None:
        """Rising maps to Summer per ontology Section 7.1."""
        assert DOMAIN_MAPPINGS[Phase.RISING.value]["season"] == "Summer"

    def test_bottoming_out_season_is_winter_solstice(self) -> None:
        """Bottoming Out maps to the Winter Solstice per ontology Section 7.1."""
        assert DOMAIN_MAPPINGS[Phase.BOTTOMING_OUT.value]["season"] == "Winter Solstice"

    def test_peaking_buddhist_attachment_is_attraction(self) -> None:
        """Peaking's Buddhist attachment value is Attraction."""
        assert (
            DOMAIN_MAPPINGS[Phase.PEAKING.value]["buddhist_attachment"] == "Attraction"
        )

    def test_diminishing_buddhist_attachment_is_aversion(self) -> None:
        """Diminishing's Buddhist attachment value is Aversion."""
        assert (
            DOMAIN_MAPPINGS[Phase.DIMINISHING.value]["buddhist_attachment"]
            == "Aversion"
        )

    def test_restoration_mood_is_depression(self) -> None:
        """Restoration in the bipolar-frame Mood column is Depression."""
        assert DOMAIN_MAPPINGS[Phase.RESTORATION.value]["mood"] == "Depression"

    def test_phase_descriptions_cover_all_six(self) -> None:
        """Every classified phase has a one-line description."""
        expected = {
            Phase.RISING.value,
            Phase.PEAKING.value,
            Phase.WITHDRAWAL.value,
            Phase.DIMINISHING.value,
            Phase.BOTTOMING_OUT.value,
            Phase.RESTORATION.value,
        }
        assert expected <= set(PHASE_DESCRIPTIONS)
        for desc in PHASE_DESCRIPTIONS.values():
            assert len(desc) > 0


# ---- generate_weekly_report ----


@pytest.fixture()
def weekly_fragments() -> list[Fragment]:
    """Fragments inside a single ISO week (2025-W02, Mon 2025-01-06)."""
    return [
        _make_fragment(
            frag_id=f"w-{i}",
            created=datetime(2025, 1, 6 + (i % 5), 12, 0, 0),
            phase=Phase.RISING,
            mode=Mode.INHABIT,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F2,
            emotional_texture=["wonder", "wonder", "grief"][i % 3 : (i % 3) + 1],
        )
        for i in range(5)
    ]


class TestGenerateWeeklyReport:
    """Tests for WavelengthTracker.generate_weekly_report."""

    def test_filename_uses_iso_year_week_format(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Weekly reports are named ``YYYY-WNN-wavelength.md``."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        # 2025-01-08 is in ISO week 02.
        assert path.name == "2025-W02-wavelength.md"

    def test_filename_week_is_zero_padded(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Single-digit ISO weeks are zero-padded (e.g. W02, not W2)."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        assert "W02" in path.name

    def test_report_under_phase_maps(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """The file lives under ``05-Wavelength/Phase-Maps/``."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        rel = path.relative_to(vault_path)
        assert rel.parts[0] == "05-Wavelength"
        assert rel.parts[1] == "Phase-Maps"

    def test_frontmatter_fields(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Frontmatter carries type, period, week, year per Issue #43."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        post = frontmatter.load(str(path))
        assert post["type"] == "wavelength_report"
        assert post["period"] == "weekly"
        assert post["week"] == 2
        assert post["year"] == 2025

    def test_body_contains_required_sections(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Body includes the seven sections mandated by Issue #43."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "Phase Summary" in content
        assert "Domain Mappings" in content
        assert "Mode Distribution" in content
        assert "Dosage Balance" in content
        assert "Emotional Texture Cloud" in content
        assert "Notable Fragments" in content
        assert "Transition Watch" in content

    def test_phase_summary_uses_ontology_description(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Phase Summary renders the ontology description for the dominant phase."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        rising_description = PHASE_DESCRIPTIONS[Phase.RISING.value]
        assert rising_description in content

    def test_domain_mappings_rendered(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Domain Mappings section surfaces Season/Mood/Breath values."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "Summer" in content
        assert "Mania" in content
        assert "Inhale end" in content

    def test_emotional_texture_cloud_format(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Texture tags render as ``tag(count)`` tokens per Issue #43."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        # wonder appears multiple times, grief at least once
        assert "wonder(" in content
        assert "grief(" in content

    def test_mode_distribution_shows_active_modes(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Mode Distribution lists every mode that appeared this week."""
        fragments = [
            _make_fragment(
                frag_id="a",
                created=datetime(2025, 1, 6, 9, 0, 0),
                phase=Phase.RISING,
                mode=Mode.INHABIT,
            ),
            _make_fragment(
                frag_id="b",
                created=datetime(2025, 1, 7, 9, 0, 0),
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
        ]
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert Mode.INHABIT.value in content
        assert Mode.EXPRESS.value in content

    def test_dosage_balance_reports_medicine_and_toxic(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Dosage Balance renders both medicine and toxic percentages."""
        fragments = [
            _make_fragment(
                frag_id="med-1",
                created=datetime(2025, 1, 6, 9, 0, 0),
                phase=Phase.RISING,
                dosage=Dosage.MEDICINE,
            ),
            _make_fragment(
                frag_id="tox-1",
                created=datetime(2025, 1, 7, 9, 0, 0),
                phase=Phase.RISING,
                dosage=Dosage.TOXIC,
            ),
        ]
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "50%" in content  # 1 medicine of 2 classified = 50%
        assert "Medicine" in content
        assert "Toxic" in content

    def test_notable_fragments_limited_to_five(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """At most 5 notable fragments are listed even when many classified."""
        fragments = [
            _make_fragment(
                frag_id=f"frag-{i}",
                title=f"notable {i}",
                created=datetime(2025, 1, 6, 9, i, 0),
                phase=Phase.RISING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                frequency=Frequency.F2,
                emotional_texture=["wonder"],
            )
            for i in range(10)
        ]
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=fragments,
        )
        content = path.read_text(encoding="utf-8")
        notable_lines = [
            line
            for line in content.splitlines()
            if line.startswith("- [[") and "notable" in line
        ]
        assert 0 < len(notable_lines) <= 5

    def test_notable_fragments_link_back_by_id(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Notable fragments are rendered as wiki-links to their IDs."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "[[w-0" in content or "[[w-1" in content

    def test_transition_watch_notes_next_possible_phase(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Transition Watch surfaces a recent shift when one exists."""
        fragments: list[Fragment] = []
        prior_week_start = datetime(2024, 12, 30, 9, 0, 0)
        # Prior week: RISING
        for day in range(3):
            fragments.append(
                _make_fragment(
                    frag_id=f"r-{day}",
                    created=prior_week_start + timedelta(days=day),
                    phase=Phase.RISING,
                    mode=Mode.INHABIT,
                    dosage=Dosage.MEDICINE,
                    frequency=Frequency.F2,
                ),
            )
        # This week: PEAKING
        for day in range(3):
            fragments.append(
                _make_fragment(
                    frag_id=f"p-{day}",
                    created=datetime(2025, 1, 6 + day, 9, 0, 0),
                    phase=Phase.PEAKING,
                    mode=Mode.EXPRESS,
                    dosage=Dosage.MEDICINE,
                    frequency=Frequency.F2,
                ),
            )
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "rising" in content.lower()
        assert "peaking" in content.lower()

    def test_dataview_section_present(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """A Dataview query scoped to this week's fragments is included."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_descriptive_not_prescriptive(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        weekly_fragments: list[Fragment],
    ) -> None:
        """Body avoids prescriptive phrasing per the ontology."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=weekly_fragments,
        )
        content = path.read_text(encoding="utf-8").lower()
        assert "you should" not in content
        assert "you must" not in content
        assert "you need to" not in content

    def test_handles_week_at_year_boundary(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """ISO weeks spanning years use the ISO calendar year, not civil year."""
        # Thursday 2026-01-01 is in ISO week 2026-W01.
        fragments = [
            _make_fragment(
                frag_id="bound",
                created=datetime(2026, 1, 1, 9, 0, 0),
                phase=Phase.RESTORATION,
            ),
        ]
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 1, 1),
            fragments=fragments,
        )
        assert path.name == "2026-W01-wavelength.md"

    def test_empty_fragments_still_generate_report(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Weekly report generation is robust to empty fragments."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2025, 1, 8),
            fragments=[],
        )
        assert path.exists()

    def test_loads_fragments_from_vault_when_none_passed(
        self,
        tracker: WavelengthTracker,
        tmp_path: Path,
    ) -> None:
        """When ``fragments`` is omitted, fragments are loaded from the vault."""
        # Seed a vault with a fragment note.
        frag_dir = tmp_path / "01-Fragments"
        frag_dir.mkdir(parents=True, exist_ok=True)
        fragment = _make_fragment(
            frag_id="from-vault-1",
            created=datetime(2025, 1, 7, 10, 0, 0),
            phase=Phase.RISING,
            mode=Mode.INHABIT,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F2,
        )
        post = frontmatter.Post(content="Body", **fragment.model_dump(mode="json"))
        (frag_dir / f"{fragment.id}.md").write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )
        path = tracker.generate_weekly_report(tmp_path, week_of=date(2025, 1, 8))
        content = path.read_text(encoding="utf-8")
        assert "from-vault-1" in content


# ---- generate_monthly_report ----


@pytest.fixture()
def monthly_fragments() -> list[Fragment]:
    """Fragments spanning four weeks of January 2025 with a phase progression."""
    fragments: list[Fragment] = []
    phase_schedule = [
        (Phase.RISING, Mode.INHABIT),
        (Phase.PEAKING, Mode.EXPRESS),
        (Phase.WITHDRAWAL, Mode.COLLABORATE),
        (Phase.DIMINISHING, Mode.INTEGRATE),
    ]
    for week_index, (phase, mode) in enumerate(phase_schedule):
        for day in range(3):
            fragments.append(
                _make_fragment(
                    frag_id=f"m-{week_index}-{day}",
                    created=datetime(2025, 1, 6 + week_index * 7 + day, 9, 0, 0),
                    phase=phase,
                    mode=mode,
                    dosage=Dosage.MEDICINE,
                    frequency=Frequency.F2,
                    emotional_texture=["wonder"],
                ),
            )
    return fragments


class TestGenerateMonthlyReport:
    """Tests for WavelengthTracker.generate_monthly_report."""

    def test_filename_uses_year_month_format(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Monthly reports are named ``YYYY-MM-wavelength.md``."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=monthly_fragments,
        )
        assert path.name == "2025-01-wavelength.md"

    def test_frontmatter_has_month_and_year(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Monthly frontmatter carries month + year, period is 'monthly'."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=monthly_fragments,
        )
        post = frontmatter.load(str(path))
        assert post["type"] == "wavelength_report"
        assert post["period"] == "monthly"
        assert post["month"] == 1
        assert post["year"] == 2025

    def test_includes_week_by_week_progression(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Monthly report renders a text-based week-by-week progression chart."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=monthly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "Phase Progression" in content
        assert "Week 1" in content
        assert "Week 2" in content

    def test_progression_chart_uses_block_marks(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Progression chart uses ``#`` blocks to visualise intensity."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=monthly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        assert "#" in content

    def test_month_over_month_comparison_section(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Monthly report surfaces a Month-over-Month comparison section."""
        # Seed prior-month fragments so comparison is non-trivial.
        previous_month = [
            _make_fragment(
                frag_id=f"prev-{i}",
                created=datetime(2024, 12, 10 + i, 9, 0, 0),
                phase=Phase.BOTTOMING_OUT,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                frequency=Frequency.F1,
            )
            for i in range(3)
        ]
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=[*monthly_fragments, *previous_month],
        )
        content = path.read_text(encoding="utf-8")
        assert "Month-over-Month" in content

    def test_monthly_body_has_same_core_sections_as_weekly(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
        monthly_fragments: list[Fragment],
    ) -> None:
        """Monthly report retains the weekly report's core sections."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 1, 1),
            fragments=monthly_fragments,
        )
        content = path.read_text(encoding="utf-8")
        for section in (
            "Phase Summary",
            "Domain Mappings",
            "Mode Distribution",
            "Dosage Balance",
            "Emotional Texture Cloud",
            "Notable Fragments",
            "Transition Watch",
        ):
            assert section in content

    def test_empty_fragments_still_generate_report(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Monthly report generation is robust to empty fragments."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2025, 2, 1),
            fragments=[],
        )
        assert path.exists()

    def test_loads_fragments_from_vault_when_none_passed(
        self,
        tracker: WavelengthTracker,
        tmp_path: Path,
    ) -> None:
        """When fragments omitted, monthly reports load them from the vault."""
        frag_dir = tmp_path / "01-Fragments"
        frag_dir.mkdir(parents=True, exist_ok=True)
        fragment = _make_fragment(
            frag_id="from-vault-monthly",
            created=datetime(2025, 1, 15, 10, 0, 0),
            phase=Phase.PEAKING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F2,
        )
        post = frontmatter.Post(content="Body", **fragment.model_dump(mode="json"))
        (frag_dir / f"{fragment.id}.md").write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )
        path = tracker.generate_monthly_report(tmp_path, month=date(2025, 1, 1))
        content = path.read_text(encoding="utf-8")
        assert "from-vault-monthly" in content
