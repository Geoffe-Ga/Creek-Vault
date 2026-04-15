"""Wavelength tracking — descriptive phase detection from fragment metadata.

Implements Section 7.5 of the Creek Ontology. The :class:`WavelengthTracker`
aggregates wavelength classifications across a time window into snapshots,
detects phase transitions between adjacent snapshots, tracks rolling medicine-
vs-toxic dosage ratios per APTITUDE frequency, and writes periodic reports to
``05-Wavelength/Phase-Maps/``.

The module is deliberately **descriptive only**. It never prescribes action,
never suggests the human "should" do anything, and never names a phase as
"better" or "worse". The point is to mirror pattern back to the observer.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import frontmatter

from creek.models import Dosage, Frequency, Mode, Phase

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment


DEFAULT_WINDOW_DAYS: int = 7
"""Default analysis window for :meth:`WavelengthTracker.analyze_period` helpers."""

DEFAULT_ROLLING_WEEKS: int = 4
"""Rolling-average window, in weeks, for dosage trend smoothing."""

DEFAULT_TOXIC_THRESHOLD: float = 0.6
"""Toxic ratio above which a frequency enters the "trending" zone."""

DEFAULT_TOXIC_CONSECUTIVE_WEEKS: int = 3
"""Number of consecutive high-toxic weeks needed to flag a frequency."""

_VALID_PERIODS: frozenset[str] = frozenset({"weekly", "monthly"})
"""Accepted period strings for :meth:`WavelengthTracker.generate_report`."""


@dataclass
class WavelengthSnapshot:
    """Descriptive summary of wavelength classifications in one time window.

    Attributes:
        start_date: Inclusive lower bound of the analysed window.
        end_date: Inclusive upper bound of the analysed window.
        dominant_phase: Most frequent Archetypal Wavelength phase, or
            ``unclassified`` when no classified fragments were found.
        dominant_mode: Most frequent engagement mode, or ``unclassified``.
        medicine_percent: Share of classified fragments marked ``medicine``.
        toxic_percent: Share of classified fragments marked ``toxic``.
        emotional_texture_cloud: ``(tag, count)`` pairs ordered by
            descending frequency.
        confidence: How consistent the dominant phase is across the
            window, in ``[0.0, 1.0]``. Zero when no classifications exist.
        fragment_count: Number of fragments that fell inside the window.
        supporting_fragment_ids: IDs of every fragment in the window.
    """

    start_date: date
    end_date: date
    dominant_phase: str
    dominant_mode: str
    medicine_percent: float
    toxic_percent: float
    emotional_texture_cloud: list[tuple[str, int]]
    confidence: float
    fragment_count: int
    supporting_fragment_ids: list[str] = field(default_factory=list)


@dataclass
class PhaseTransition:
    """A detected shift in dominant phase between two adjacent snapshots.

    Attributes:
        from_phase: Dominant phase of the earlier snapshot.
        to_phase: Dominant phase of the later snapshot.
        from_date: End date of the earlier snapshot.
        to_date: Start date of the later snapshot.
        supporting_fragment_ids: Fragment IDs from both snapshots that
            witnessed the transition.
    """

    from_phase: str
    to_phase: str
    from_date: date
    to_date: date
    supporting_fragment_ids: list[str] = field(default_factory=list)


@dataclass
class DosageTrend:
    """Rolling medicine-vs-toxic dosage ratios per APTITUDE frequency.

    Attributes:
        frequency_trends: For each classified frequency, a list of
            ``(week_start_date, toxic_ratio)`` pairs forming a rolling
            average over :attr:`WavelengthTracker.rolling_weeks` weeks.
        flagged_frequencies: Frequencies whose rolling toxic ratio stays
            above :attr:`WavelengthTracker.toxic_threshold` for at least
            :attr:`WavelengthTracker.consecutive_weeks` consecutive weeks.
    """

    frequency_trends: dict[str, list[tuple[date, float]]] = field(
        default_factory=dict,
    )
    flagged_frequencies: list[str] = field(default_factory=list)


def _week_start(day: date) -> date:
    """Return the Monday of the ISO week containing *day*."""
    return day - timedelta(days=day.weekday())


def _fragment_in_window(fragment: Fragment, start: date, end: date) -> bool:
    """Return whether *fragment* was created within ``[start, end]`` inclusive."""
    frag_date = fragment.created.date()
    return start <= frag_date <= end


def _most_common_classified(values: list[str], unclassified: str) -> str:
    """Return the most common *values* entry, ignoring *unclassified* markers."""
    classified = [v for v in values if v and v != unclassified]
    if not classified:
        return unclassified
    counter = Counter(classified)
    return counter.most_common(1)[0][0]


def _ratio(numerator: int, denominator: int) -> float:
    """Return ``numerator / denominator`` or ``0.0`` when denominator is zero."""
    if denominator == 0:
        return 0.0
    return numerator / denominator


class WavelengthTracker:
    """Aggregate, observe, and describe wavelength patterns across time.

    The tracker never prescribes. Every method produces a descriptive
    artifact: a snapshot, a transition, a trend, or a markdown report.

    Attributes:
        window_days: Length of the analysis window used by snapshot and
            weekly aggregation helpers.
        rolling_weeks: Rolling-average window, in weeks, for dosage trend
            smoothing.
        toxic_threshold: Toxic ratio above which a frequency enters the
            trending zone.
        consecutive_weeks: Number of consecutive high-toxic weeks needed
            to flag a frequency.
    """

    def __init__(
        self,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        rolling_weeks: int = DEFAULT_ROLLING_WEEKS,
        toxic_threshold: float = DEFAULT_TOXIC_THRESHOLD,
        consecutive_weeks: int = DEFAULT_TOXIC_CONSECUTIVE_WEEKS,
    ) -> None:
        """Initialise the tracker.

        Args:
            window_days: Length of the snapshot window. Defaults to 7.
            rolling_weeks: Weeks in the dosage rolling average. Defaults to 4.
            toxic_threshold: Ratio above which a frequency is "trending
                toward overdose". Defaults to 0.6.
            consecutive_weeks: Consecutive weeks above ``toxic_threshold``
                required to flag a frequency. Defaults to 3.
        """
        self.window_days = window_days
        self.rolling_weeks = rolling_weeks
        self.toxic_threshold = toxic_threshold
        self.consecutive_weeks = consecutive_weeks

    # ---- analyze_period ----

    def analyze_period(
        self,
        fragments: list[Fragment],
        start: date,
        end: date,
    ) -> WavelengthSnapshot:
        """Aggregate *fragments* in ``[start, end]`` into a descriptive snapshot.

        Args:
            fragments: Fragments to analyse. Those outside the window are
                silently skipped.
            start: Inclusive lower bound (date).
            end: Inclusive upper bound (date).

        Returns:
            A :class:`WavelengthSnapshot` describing the period.
        """
        in_window = [f for f in fragments if _fragment_in_window(f, start, end)]
        phases = [str(f.wavelength.phase) for f in in_window]
        modes = [str(f.wavelength.mode) for f in in_window]
        dosages = [str(f.wavelength.dosage) for f in in_window]

        dominant_phase = _most_common_classified(phases, Phase.UNCLASSIFIED.value)
        dominant_mode = _most_common_classified(modes, Mode.UNCLASSIFIED.value)
        medicine, toxic = self._dosage_shares(dosages)
        texture = self._texture_cloud(in_window)
        confidence = self._phase_confidence(phases, dominant_phase)

        return WavelengthSnapshot(
            start_date=start,
            end_date=end,
            dominant_phase=dominant_phase,
            dominant_mode=dominant_mode,
            medicine_percent=medicine,
            toxic_percent=toxic,
            emotional_texture_cloud=texture,
            confidence=confidence,
            fragment_count=len(in_window),
            supporting_fragment_ids=[f.id for f in in_window],
        )

    @staticmethod
    def _dosage_shares(dosages: list[str]) -> tuple[float, float]:
        """Return ``(medicine_share, toxic_share)`` over classified dosages."""
        classified = [d for d in dosages if d and d != Dosage.UNCLASSIFIED.value]
        total = len(classified)
        medicine = sum(1 for d in classified if d == Dosage.MEDICINE.value)
        toxic = sum(1 for d in classified if d == Dosage.TOXIC.value)
        return _ratio(medicine, total), _ratio(toxic, total)

    @staticmethod
    def _texture_cloud(fragments: list[Fragment]) -> list[tuple[str, int]]:
        """Return emotional texture tags counted and sorted by descending count."""
        counter: Counter[str] = Counter()
        for fragment in fragments:
            counter.update(fragment.emotional_texture)
        return counter.most_common()

    @staticmethod
    def _phase_confidence(phases: list[str], dominant_phase: str) -> float:
        """Return the share of classified phases that match *dominant_phase*."""
        classified = [p for p in phases if p and p != Phase.UNCLASSIFIED.value]
        if not classified or dominant_phase == Phase.UNCLASSIFIED.value:
            return 0.0
        matching = sum(1 for p in classified if p == dominant_phase)
        return matching / len(classified)

    # ---- detect_transitions ----

    def detect_transitions(
        self,
        snapshots: list[WavelengthSnapshot],
    ) -> list[PhaseTransition]:
        """Compare adjacent snapshots and emit a transition when the phase shifts.

        Unclassified dominant phases are skipped on either side of the pair:
        a transition requires both snapshots to have a classified phase.

        Args:
            snapshots: Snapshots in chronological order.

        Returns:
            List of :class:`PhaseTransition` objects.
        """
        transitions: list[PhaseTransition] = []
        unclassified = Phase.UNCLASSIFIED.value
        for earlier, later in itertools.pairwise(snapshots):
            if unclassified in (earlier.dominant_phase, later.dominant_phase):
                continue
            if earlier.dominant_phase == later.dominant_phase:
                continue
            transitions.append(
                PhaseTransition(
                    from_phase=earlier.dominant_phase,
                    to_phase=later.dominant_phase,
                    from_date=earlier.end_date,
                    to_date=later.start_date,
                    supporting_fragment_ids=[
                        *earlier.supporting_fragment_ids,
                        *later.supporting_fragment_ids,
                    ],
                ),
            )
        return transitions

    # ---- track_dosage_trends ----

    def track_dosage_trends(self, fragments: list[Fragment]) -> DosageTrend:
        """Compute rolling toxic ratios per frequency and flag overdose trends.

        For each classified frequency the tracker builds weekly buckets,
        computes the toxic share within each bucket, then smooths the
        series with a trailing rolling average of :attr:`rolling_weeks`
        weeks. A frequency is flagged when its rolling average sits above
        :attr:`toxic_threshold` for at least :attr:`consecutive_weeks`
        consecutive weeks.

        Args:
            fragments: Fragments to analyse.

        Returns:
            A :class:`DosageTrend` with per-frequency series and a flag list.
        """
        grouped = self._group_by_frequency(fragments)
        trends: dict[str, list[tuple[date, float]]] = {}
        flagged: list[str] = []
        for frequency, frags in grouped.items():
            weekly = self._weekly_toxic_ratios(frags)
            rolling = self._rolling_average(weekly)
            trends[frequency] = rolling
            if self._is_trending_toxic(rolling):
                flagged.append(frequency)
        return DosageTrend(frequency_trends=trends, flagged_frequencies=flagged)

    @staticmethod
    def _group_by_frequency(
        fragments: list[Fragment],
    ) -> dict[str, list[Fragment]]:
        """Group *fragments* by their classified primary frequency."""
        grouped: dict[str, list[Fragment]] = defaultdict(list)
        for fragment in fragments:
            freq = str(fragment.frequency.primary)
            if not freq or freq == Frequency.UNCLASSIFIED.value:
                continue
            grouped[freq].append(fragment)
        return grouped

    @staticmethod
    def _weekly_toxic_ratios(
        fragments: list[Fragment],
    ) -> list[tuple[date, float]]:
        """Return ``(week_start, toxic_ratio)`` for each week spanned by fragments."""
        buckets: dict[date, list[str]] = defaultdict(list)
        for fragment in fragments:
            dosage = str(fragment.wavelength.dosage)
            if not dosage or dosage == Dosage.UNCLASSIFIED.value:
                continue
            week = _week_start(fragment.created.date())
            buckets[week].append(dosage)
        weekly: list[tuple[date, float]] = []
        for week in sorted(buckets):
            dosages = buckets[week]
            toxic = sum(1 for d in dosages if d == Dosage.TOXIC.value)
            weekly.append((week, _ratio(toxic, len(dosages))))
        return weekly

    def _rolling_average(
        self,
        weekly: list[tuple[date, float]],
    ) -> list[tuple[date, float]]:
        """Apply a trailing *rolling_weeks* mean to *weekly* toxic ratios."""
        if not weekly:
            return []
        window = max(1, self.rolling_weeks)
        result: list[tuple[date, float]] = []
        for idx in range(len(weekly)):
            week = weekly[idx][0]
            start = max(0, idx - window + 1)
            sample = [r for _, r in weekly[start : idx + 1]]
            result.append((week, sum(sample) / len(sample)))
        return result

    def _is_trending_toxic(self, rolling: list[tuple[date, float]]) -> bool:
        """Return whether *rolling* has *consecutive_weeks* above the threshold."""
        streak = 0
        for _, ratio in rolling:
            if ratio > self.toxic_threshold:
                streak += 1
                if streak >= self.consecutive_weeks:
                    return True
            else:
                streak = 0
        return False

    # ---- generate_report ----

    def generate_report(
        self,
        vault_path: Path,
        period: str,
        fragments: list[Fragment],
    ) -> Path:
        """Write a descriptive wavelength report to ``05-Wavelength/Phase-Maps/``.

        The report aggregates *fragments* into weekly snapshots, detects
        transitions, tracks dosage trends, and renders the five required
        sections: Current Phase, Phase History, Dosage Trends, Transition
        Log, and Emotional Texture Cloud. Language is descriptive only.

        Args:
            vault_path: Root of the Obsidian vault.
            period: Either ``"weekly"`` or ``"monthly"``. Controls the
                report title and filename prefix.
            fragments: Fragments to aggregate. Empty lists still produce a
                valid report.

        Returns:
            Path to the written markdown report.

        Raises:
            ValueError: If *period* is not ``"weekly"`` or ``"monthly"``.
        """
        if period not in _VALID_PERIODS:
            msg = f"Unknown period {period!r}; expected one of {sorted(_VALID_PERIODS)}"
            raise ValueError(msg)

        target_dir = vault_path / "05-Wavelength" / "Phase-Maps"
        target_dir.mkdir(parents=True, exist_ok=True)

        snapshots = self._weekly_snapshots(fragments)
        transitions = self.detect_transitions(snapshots)
        trend = self.track_dosage_trends(fragments)

        today = date.today()
        body = self._render_report_body(snapshots, transitions, trend)
        post = frontmatter.Post(
            content=body,
            type="wavelength-report",
            period=period,
            generated_on=today.isoformat(),
            window_days=self.window_days,
            flagged_frequencies=list(trend.flagged_frequencies),
            tags=["wavelength", f"wavelength-{period}"],
        )

        note_path = target_dir / f"{today.isoformat()}-{period}.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def _weekly_snapshots(
        self,
        fragments: list[Fragment],
    ) -> list[WavelengthSnapshot]:
        """Build contiguous weekly snapshots spanning the fragment date range."""
        if not fragments:
            return []
        first = min(f.created.date() for f in fragments)
        last = max(f.created.date() for f in fragments)
        start = _week_start(first)
        snapshots: list[WavelengthSnapshot] = []
        while start <= last:
            end = start + timedelta(days=self.window_days - 1)
            snapshots.append(self.analyze_period(fragments, start, end))
            start = end + timedelta(days=1)
        return snapshots

    @staticmethod
    def _render_report_body(
        snapshots: list[WavelengthSnapshot],
        transitions: list[PhaseTransition],
        trend: DosageTrend,
    ) -> str:
        """Render the full markdown body for the report."""
        lines: list[str] = []
        lines.extend(_render_current_phase(snapshots))
        lines.extend(_render_phase_history(snapshots))
        lines.extend(_render_dosage_trends(trend))
        lines.extend(_render_transition_log(transitions))
        lines.extend(_render_texture_cloud(snapshots))
        lines.extend(_render_dataview_section())
        return "\n".join(lines)


# ---- Report rendering helpers ----


def _render_current_phase(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render the ``Current Phase`` section describing the latest snapshot."""
    lines = ["## Current Phase", ""]
    if not snapshots:
        lines.append("_No fragments available for this period._")
        lines.append("")
        return lines
    current = snapshots[-1]
    lines.append(
        f"- Dominant phase: **{current.dominant_phase}** "
        f"(confidence {current.confidence:.2f})",
    )
    lines.append(f"- Dominant mode: **{current.dominant_mode}**")
    lines.append(f"- Fragment count: {current.fragment_count}")
    lines.append(
        f"- Medicine share: {current.medicine_percent:.2f} | "
        f"Toxic share: {current.toxic_percent:.2f}",
    )
    lines.append("")
    return lines


def _render_phase_history(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render one bullet per snapshot as a chronological history."""
    lines = ["## Phase History", ""]
    if not snapshots:
        lines.append("_No snapshots recorded._")
        lines.append("")
        return lines
    for snap in snapshots:
        lines.append(
            f"- {snap.start_date.isoformat()} → {snap.end_date.isoformat()}: "
            f"{snap.dominant_phase} ({snap.fragment_count} fragments)",
        )
    lines.append("")
    return lines


def _render_dosage_trends(trend: DosageTrend) -> list[str]:
    """Render the Dosage Trends section, listing flagged frequencies neutrally."""
    lines = ["## Dosage Trends", ""]
    if not trend.frequency_trends:
        lines.append("_No classified frequencies recorded._")
        lines.append("")
        return lines
    for freq in sorted(trend.frequency_trends):
        series = trend.frequency_trends[freq]
        if not series:
            continue
        latest_week, latest_ratio = series[-1]
        lines.append(
            f"- **{freq}**: latest rolling toxic share "
            f"{latest_ratio:.2f} (week of {latest_week.isoformat()})",
        )
    if trend.flagged_frequencies:
        lines.append("")
        lines.append("Frequencies trending toward overdose (descriptive signal):")
        for freq in sorted(trend.flagged_frequencies):
            lines.append(f"- {freq}")
    lines.append("")
    return lines


def _render_transition_log(transitions: list[PhaseTransition]) -> list[str]:
    """Render one bullet per detected phase transition."""
    lines = ["## Transition Log", ""]
    if not transitions:
        lines.append("_No phase transitions detected in this period._")
        lines.append("")
        return lines
    for trans in transitions:
        lines.append(
            f"- {trans.from_date.isoformat()} → {trans.to_date.isoformat()}: "
            f"{trans.from_phase} → {trans.to_phase}",
        )
    lines.append("")
    return lines


def _render_texture_cloud(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render an emotional texture cloud aggregated across all snapshots."""
    lines = ["## Emotional Texture Cloud", ""]
    merged: Counter[str] = Counter()
    for snap in snapshots:
        for tag, count in snap.emotional_texture_cloud:
            merged[tag] += count
    if not merged:
        lines.append("_No emotional texture tags recorded._")
        lines.append("")
        return lines
    for tag, count in merged.most_common():
        lines.append(f"- `{tag}` x{count}")
    lines.append("")
    return lines


def _render_dataview_section() -> list[str]:
    """Render a Dataview query block for fragments grouped by phase."""
    return [
        "## Fragments by Phase",
        "",
        "```dataview",
        "TABLE wavelength.phase AS Phase, created AS Created",
        'FROM "01-Fragments"',
        'WHERE type = "fragment"',
        "SORT created DESC",
        "```",
        "",
    ]


__all__ = [
    "DEFAULT_ROLLING_WEEKS",
    "DEFAULT_TOXIC_CONSECUTIVE_WEEKS",
    "DEFAULT_TOXIC_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "DosageTrend",
    "PhaseTransition",
    "WavelengthSnapshot",
    "WavelengthTracker",
]
