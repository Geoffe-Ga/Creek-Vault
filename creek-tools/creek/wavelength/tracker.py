"""Wavelength tracker — temporal analysis of fragment classifications.

Analyzes classified fragments within configurable time windows to:

1. **Detect dominant phases** — the most frequent wavelength phase in each window
2. **Detect phase transitions** — when the dominant phase changes between windows
3. **Track dosage trends** — ratio of toxic vs medicine dosage across fragments
4. **Generate observations** — produce :class:`WavelengthObservation` records

The tracker is descriptive, never prescriptive: it surfaces patterns
without recommending what the human "should" do.

Section 7.5 of the Creek Ontology specifies these requirements.
"""

import logging
from collections import Counter
from datetime import date, timedelta

from pydantic import BaseModel, Field

from creek.models import (
    Confidence,
    Dosage,
    Fragment,
    Mode,
    Phase,
    WavelengthObservation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class WindowSummary(BaseModel):
    """Summary of wavelength state within a single time window.

    Attributes:
        start: First date of the window (inclusive).
        end: Last date of the window (inclusive).
        dominant_phase: Most frequent phase among fragments in the window.
        dominant_mode: Most frequent mode among fragments in the window.
        phase_counts: Mapping of phase to fragment count.
        mode_counts: Mapping of mode to fragment count.
        dosage_counts: Mapping of dosage to fragment count.
        fragment_count: Total number of classified fragments in the window.
        toxic_ratio: Ratio of toxic-dosed fragments to all dosed fragments.
    """

    start: date
    end: date
    dominant_phase: Phase = Phase.UNCLASSIFIED
    dominant_mode: Mode = Mode.UNCLASSIFIED
    phase_counts: dict[str, int] = Field(default_factory=dict)
    mode_counts: dict[str, int] = Field(default_factory=dict)
    dosage_counts: dict[str, int] = Field(default_factory=dict)
    fragment_count: int = 0
    toxic_ratio: float = 0.0


class PhaseTransition(BaseModel):
    """A detected shift in dominant phase between consecutive windows.

    Attributes:
        from_phase: The dominant phase in the earlier window.
        to_phase: The dominant phase in the later window.
        transition_date: The start date of the window where the new phase began.
    """

    from_phase: Phase
    to_phase: Phase
    transition_date: date


class DosageTrend(BaseModel):
    """Dosage trend across a sequence of time windows.

    Attributes:
        window_ratios: Ordered list of (window_start, toxic_ratio) pairs.
        trend: ``rising`` if toxic ratio is increasing across windows,
            ``falling`` if decreasing, ``stable`` otherwise.
        latest_ratio: The toxic ratio in the most recent window.
    """

    window_ratios: list[tuple[date, float]] = Field(default_factory=list)
    trend: str = "stable"
    latest_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class WavelengthTracker:
    """Analyze classified fragments over time windows.

    Groups fragments by creation date into windows of ``window_days``
    length, computes dominant phases and modes per window, and detects
    phase transitions and dosage trends.

    Attributes:
        window_days: Number of days per analysis window.
        min_fragments: Minimum fragments in a window for it to be considered.
    """

    def __init__(
        self,
        *,
        window_days: int = 7,
        min_fragments: int = 1,
    ) -> None:
        """Initialise the tracker with configurable parameters.

        Args:
            window_days: Width of each time window in days.
            min_fragments: Minimum number of fragments required for a
                window to produce a summary.
        """
        self.window_days = window_days
        self.min_fragments = min_fragments

    def summarize_windows(
        self,
        fragments: list[Fragment],
    ) -> list[WindowSummary]:
        """Group fragments into time windows and summarize each.

        Fragments are grouped by their ``created`` date into consecutive
        windows of ``window_days`` length.  Empty windows (or windows
        below ``min_fragments``) are omitted.

        Args:
            fragments: Classified fragments to analyze.

        Returns:
            Ordered list of :class:`WindowSummary` objects, earliest first.
        """
        if not fragments:
            return []

        buckets = self._bucket_fragments(fragments)
        summaries: list[WindowSummary] = []

        for window_start in sorted(buckets):
            bucket = buckets[window_start]
            if len(bucket) < self.min_fragments:
                continue

            summary = self._summarize_bucket(window_start, bucket)
            summaries.append(summary)

        return summaries

    def detect_transitions(
        self,
        summaries: list[WindowSummary],
    ) -> list[PhaseTransition]:
        """Detect phase transitions between consecutive window summaries.

        A transition is recorded whenever the dominant phase changes
        between two adjacent windows (ignoring ``UNCLASSIFIED``).

        Args:
            summaries: Ordered list of window summaries.

        Returns:
            List of :class:`PhaseTransition` objects.
        """
        transitions: list[PhaseTransition] = []

        prev_phase: Phase | None = None
        for summary in summaries:
            current = summary.dominant_phase
            if current == Phase.UNCLASSIFIED:
                continue
            if prev_phase is not None and current != prev_phase:
                transitions.append(
                    PhaseTransition(
                        from_phase=prev_phase,
                        to_phase=current,
                        transition_date=summary.start,
                    )
                )
            prev_phase = current

        return transitions

    def compute_dosage_trend(
        self,
        summaries: list[WindowSummary],
    ) -> DosageTrend:
        """Compute the dosage trend across time windows.

        Examines the toxic ratio in each window and determines whether
        the trend is ``rising``, ``falling``, or ``stable``.

        The trend is determined by comparing the first and last thirds
        of the window sequence.  If the average toxic ratio of the last
        third exceeds the first by more than 0.1, the trend is ``rising``.
        If it is lower by more than 0.1, the trend is ``falling``.

        Args:
            summaries: Ordered list of window summaries.

        Returns:
            A :class:`DosageTrend` with per-window ratios and trend direction.
        """
        window_ratios = [
            (s.start, s.toxic_ratio) for s in summaries if s.fragment_count > 0
        ]

        if not window_ratios:
            return DosageTrend()

        latest_ratio = window_ratios[-1][1]
        trend = self._determine_trend(window_ratios)

        return DosageTrend(
            window_ratios=window_ratios,
            trend=trend,
            latest_ratio=latest_ratio,
        )

    def generate_observations(
        self,
        summaries: list[WindowSummary],
    ) -> list[WavelengthObservation]:
        """Generate WavelengthObservation records from window summaries.

        Creates one observation per window, capturing the dominant phase,
        mode, and dosage.  Only windows with a classified dominant phase
        produce observations.

        Args:
            summaries: Ordered list of window summaries.

        Returns:
            List of :class:`WavelengthObservation` records.
        """
        observations: list[WavelengthObservation] = []

        for summary in summaries:
            if summary.dominant_phase == Phase.UNCLASSIFIED:
                continue

            dominant_dosage = self._dominant_dosage(summary.dosage_counts)
            confidence = self._estimate_confidence(summary)

            obs = WavelengthObservation(
                date=summary.start,
                phase=summary.dominant_phase,
                mode=summary.dominant_mode,
                dosage=dominant_dosage,
                confidence=confidence,
                notes=(
                    f"Window {summary.start} to {summary.end}: "
                    f"{summary.fragment_count} fragments, "
                    f"toxic ratio {summary.toxic_ratio:.0%}"
                ),
            )
            observations.append(obs)

        return observations

    # ---- Internal helpers ----

    def _bucket_fragments(
        self,
        fragments: list[Fragment],
    ) -> dict[date, list[Fragment]]:
        """Group fragments into date-based buckets.

        Each bucket key is the start date of its window.  The window
        start is computed by flooring the fragment's creation date to the
        nearest ``window_days`` boundary relative to the earliest fragment.

        Args:
            fragments: Fragments to bucket.

        Returns:
            Mapping of window start date to list of fragments.
        """
        dates = [f.created.date() for f in fragments]
        earliest = min(dates)

        buckets: dict[date, list[Fragment]] = {}
        for fragment in fragments:
            frag_date = fragment.created.date()
            days_offset = (frag_date - earliest).days
            window_index = days_offset // self.window_days
            window_start = earliest + timedelta(days=window_index * self.window_days)

            if window_start not in buckets:
                buckets[window_start] = []
            buckets[window_start].append(fragment)

        return buckets

    def _summarize_bucket(
        self,
        window_start: date,
        fragments: list[Fragment],
    ) -> WindowSummary:
        """Summarize a bucket of fragments into a WindowSummary.

        Args:
            window_start: Start date of the window.
            fragments: Fragments in this window.

        Returns:
            A :class:`WindowSummary` for the window.
        """
        window_end = window_start + timedelta(days=self.window_days - 1)

        phase_counter: Counter[str] = Counter()
        mode_counter: Counter[str] = Counter()
        dosage_counter: Counter[str] = Counter()

        for frag in fragments:
            phase_counter[frag.wavelength.phase] += 1
            mode_counter[frag.wavelength.mode] += 1
            dosage_counter[frag.wavelength.dosage] += 1

        dominant_phase = self._pick_dominant(phase_counter, Phase.UNCLASSIFIED)
        dominant_mode = self._pick_dominant(mode_counter, Mode.UNCLASSIFIED)
        toxic_ratio = self._compute_toxic_ratio(dosage_counter)

        return WindowSummary(
            start=window_start,
            end=window_end,
            dominant_phase=Phase(dominant_phase),
            dominant_mode=Mode(dominant_mode),
            phase_counts=dict(phase_counter),
            mode_counts=dict(mode_counter),
            dosage_counts=dict(dosage_counter),
            fragment_count=len(fragments),
            toxic_ratio=toxic_ratio,
        )

    def _pick_dominant(
        self,
        counter: Counter[str],
        unclassified_value: str,
    ) -> str:
        """Pick the most common value, excluding unclassified.

        If all entries are unclassified, returns the unclassified value.

        Args:
            counter: Counter of classified values.
            unclassified_value: The string value representing unclassified.

        Returns:
            The most common non-unclassified value, or unclassified.
        """
        classified = {k: v for k, v in counter.items() if k != unclassified_value}
        if not classified:
            return unclassified_value
        return max(classified, key=lambda k: classified[k])

    def _compute_toxic_ratio(self, dosage_counter: Counter[str]) -> float:
        """Compute the ratio of toxic to all classified dosage counts.

        Unclassified dosages are excluded from the denominator.

        Args:
            dosage_counter: Counter of dosage values.

        Returns:
            Ratio in [0.0, 1.0], or 0.0 if no classified dosages.
        """
        classified_total = sum(
            v for k, v in dosage_counter.items() if k != Dosage.UNCLASSIFIED
        )
        if classified_total == 0:
            return 0.0

        toxic_count = dosage_counter.get(Dosage.TOXIC, 0)
        return round(toxic_count / classified_total, 4)

    def _dominant_dosage(self, dosage_counts: dict[str, int]) -> Dosage:
        """Pick the dominant dosage from counts, excluding unclassified.

        Args:
            dosage_counts: Mapping of dosage value to count.

        Returns:
            The most frequent classified dosage, or ``UNCLASSIFIED``.
        """
        classified = {
            k: v for k, v in dosage_counts.items() if k != Dosage.UNCLASSIFIED
        }
        if not classified:
            return Dosage.UNCLASSIFIED
        return Dosage(max(classified, key=lambda k: classified[k]))

    def _estimate_confidence(self, summary: WindowSummary) -> Confidence:
        """Estimate confidence based on fragment count and phase agreement.

        More fragments and stronger phase dominance yield higher confidence.

        Args:
            summary: The window summary to evaluate.

        Returns:
            A :class:`Confidence` level.
        """
        agreement = self._phase_agreement(summary)
        if agreement < 0:
            return Confidence.MUSING

        count = summary.fragment_count
        return self._confidence_from_count_and_agreement(count, agreement)

    def _phase_agreement(self, summary: WindowSummary) -> float:
        """Compute the fraction of classified fragments agreeing on the dominant phase.

        Args:
            summary: The window summary to evaluate.

        Returns:
            Agreement ratio in [0.0, 1.0], or -1.0 if there are no
            classified phase counts.
        """
        phase_counts = {
            k: v for k, v in summary.phase_counts.items() if k != Phase.UNCLASSIFIED
        }
        total = sum(phase_counts.values()) if phase_counts else 0
        if total == 0:
            return -1.0
        return max(phase_counts.values()) / total

    @staticmethod
    def _confidence_from_count_and_agreement(
        count: int,
        agreement: float,
    ) -> Confidence:
        """Map fragment count and agreement ratio to a confidence level.

        Uses a threshold table: each entry is (min_count, min_agreement,
        confidence).  The first matching entry wins.

        Args:
            count: Number of fragments in the window.
            agreement: Phase agreement ratio in [0.0, 1.0].

        Returns:
            The appropriate :class:`Confidence` level.
        """
        thresholds: list[tuple[int, float, Confidence]] = [
            (10, 0.7, Confidence.CONVICTION),
            (5, 0.6, Confidence.SETTLED),
            (3, 0.5, Confidence.FORMING),
            (2, 0.0, Confidence.EXPLORING),
        ]
        for min_count, min_agreement, confidence in thresholds:
            if count >= min_count and agreement >= min_agreement:
                return confidence
        return Confidence.MUSING

    def _determine_trend(
        self,
        window_ratios: list[tuple[date, float]],
    ) -> str:
        """Determine dosage trend from window ratios.

        Compares the average toxic ratio of the first third vs the last
        third of windows.

        Args:
            window_ratios: Ordered list of (date, toxic_ratio) pairs.

        Returns:
            ``rising``, ``falling``, or ``stable``.
        """
        if len(window_ratios) < 3:
            return "stable"

        third = max(len(window_ratios) // 3, 1)
        first_avg = sum(r for _, r in window_ratios[:third]) / third
        last_avg = sum(r for _, r in window_ratios[-third:]) / third

        diff = last_avg - first_avg
        if diff > 0.1:
            return "rising"
        if diff < -0.1:
            return "falling"
        return "stable"
