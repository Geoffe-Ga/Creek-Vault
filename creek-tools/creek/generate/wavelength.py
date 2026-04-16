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

import calendar
import itertools
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import frontmatter
from pydantic import ValidationError

from creek.models import Dosage, Fragment, Frequency, Mode, Phase

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


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

NOTABLE_FRAGMENT_LIMIT: int = 5
"""Maximum number of Notable Fragments rendered in a detailed report."""

PROGRESSION_CHART_MAX_WIDTH: int = 12
"""Maximum ``#`` blocks used to visualise a single week's intensity."""


PHASE_DESCRIPTIONS: dict[str, str] = {
    Phase.RISING.value: (
        "Energy building, ideas forming, momentum gathering. "
        "Abundance begins to create Indulgence."
    ),
    Phase.PEAKING.value: (
        "Full expression, maximum creative and spiritual output. Abundance peaks."
    ),
    Phase.WITHDRAWAL.value: (
        "Energy shifts, first cracks appear, turning inward begins. "
        "Indulgence creates Scarcity."
    ),
    Phase.DIMINISHING.value: (
        "Active decline, contraction, things falling apart. "
        "Scarcity begins to create Resilience."
    ),
    Phase.BOTTOMING_OUT.value: (
        "Lowest point, maximum contraction, dark-night material. Scarcity peaks."
    ),
    Phase.RESTORATION.value: (
        "Return begins, new energy gathering, re-emergence. "
        "Resilience creates Abundance."
    ),
}
"""One-line description per phase (ontology Section 7.1 narrative column)."""


DOMAIN_MAPPINGS: dict[str, dict[str, str]] = {
    Phase.RISING.value: {
        "season": "Summer",
        "mood": "Mania",
        "spaciousness": "Expanded",
        "relation_to_others": "Belonging",
        "relation_to_self": "Esteem",
        "buddhist_attachment": "Attraction",
        "meditation": "Redirecting attention",
        "breath": "Inhale end",
    },
    Phase.PEAKING.value: {
        "season": "Summer Solstice",
        "mood": "Mania",
        "spaciousness": "Expanded",
        "relation_to_others": "Belonging",
        "relation_to_self": "Esteem",
        "buddhist_attachment": "Attraction",
        "meditation": "Absorption",
        "breath": "Hold in",
    },
    Phase.WITHDRAWAL.value: {
        "season": "Fall",
        "mood": "Mania",
        "spaciousness": "Expanded",
        "relation_to_others": "Alienation",
        "relation_to_self": "Doubt",
        "buddhist_attachment": "Aversion",
        "meditation": "Distraction",
        "breath": "Exhale begin",
    },
    Phase.DIMINISHING.value: {
        "season": "Winter",
        "mood": "Depression",
        "spaciousness": "Contracted",
        "relation_to_others": "Alienation",
        "relation_to_self": "Doubt",
        "buddhist_attachment": "Aversion",
        "meditation": "Forgetting",
        "breath": "Exhale end",
    },
    Phase.BOTTOMING_OUT.value: {
        "season": "Winter Solstice",
        "mood": "Depression",
        "spaciousness": "Contracted",
        "relation_to_others": "Alienation",
        "relation_to_self": "Doubt",
        "buddhist_attachment": "Aversion",
        "meditation": "Mind wandering",
        "breath": "Hold out",
    },
    Phase.RESTORATION.value: {
        "season": "Spring",
        "mood": "Depression",
        "spaciousness": "Contracted",
        "relation_to_others": "Belonging",
        "relation_to_self": "Esteem",
        "buddhist_attachment": "Attraction",
        "meditation": "Waking Up",
        "breath": "Inhale begin",
    },
}
"""Per-phase domain mapping from ontology Section 7.1."""


_DOMAIN_LABELS: tuple[tuple[str, str], ...] = (
    ("season", "Season"),
    ("mood", "Mood (bipolar frame)"),
    ("spaciousness", "Spaciousness"),
    ("relation_to_others", "Relation to Others"),
    ("relation_to_self", "Relation to Self"),
    ("buddhist_attachment", "Buddhist Attachment"),
    ("meditation", "Meditation"),
    ("breath", "Breath"),
)
"""Ordered ``(key, human-readable-label)`` pairs for rendering domain mappings."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
"""Subdirectory of the vault where Fragment markdown notes live."""


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
                Must be a positive integer.
            rolling_weeks: Weeks in the dosage rolling average. Defaults to 4.
                Must be a positive integer.
            toxic_threshold: Ratio above which a frequency is "trending
                toward overdose". Defaults to 0.6. Must be in ``(0.0, 1.0]``;
                values outside that range would either flag every frequency
                vacuously or flag none at all.
            consecutive_weeks: Consecutive weeks above ``toxic_threshold``
                required to flag a frequency. Defaults to 3. Must be a
                positive integer.

        Raises:
            ValueError: If ``window_days``, ``rolling_weeks``, or
                ``consecutive_weeks`` is less than 1, or if
                ``toxic_threshold`` is outside ``(0.0, 1.0]``.
        """
        if window_days < 1:
            msg = f"window_days must be >= 1, got {window_days}"
            raise ValueError(msg)
        if rolling_weeks < 1:
            msg = f"rolling_weeks must be >= 1, got {rolling_weeks}"
            raise ValueError(msg)
        if consecutive_weeks < 1:
            msg = f"consecutive_weeks must be >= 1, got {consecutive_weeks}"
            raise ValueError(msg)
        if not 0.0 < toxic_threshold <= 1.0:
            msg = f"toxic_threshold must be in (0.0, 1.0], got {toxic_threshold}"
            raise ValueError(msg)
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

        Dosage buckets always use ISO calendar weeks (Monday boundaries),
        independent of :attr:`window_days`. This is deliberate: the
        ontology specifies weekly dosage accounting, and snapshot windows
        are allowed to span multiple or fractional weeks.

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
        return dict(grouped)

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
        result: list[tuple[date, float]] = []
        for idx, (week, _ratio) in enumerate(weekly):
            start = max(0, idx - self.rolling_weeks + 1)
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
        *,
        report_date: date | None = None,
    ) -> Path:
        """Write a descriptive wavelength report to ``05-Wavelength/Phase-Maps/``.

        The report aggregates *fragments* into window-sized snapshots (each
        snapshot spans :attr:`window_days` days), detects transitions, tracks
        dosage trends (bucketed by ISO calendar week; see
        :meth:`track_dosage_trends`), and renders six sections: Current
        Phase, Phase History, Dosage Trends, Transition Log, Emotional
        Texture Cloud, and a Dataview query over all fragments. Language
        is descriptive only.

        The report file is named ``<report_date>-<period>.md``; if a file
        with that name already exists it is overwritten.

        ``period`` controls the filename suffix, the ``period`` frontmatter
        value, and the tag, but it does **not** change the analysis
        granularity — both ``"weekly"`` and ``"monthly"`` runs aggregate
        over :attr:`window_days`-sized windows. Callers who want coarser
        monthly aggregation should construct the tracker with a larger
        ``window_days`` (e.g. 28 or 30) when writing a monthly report.

        Performance note: this method does
        ``O(fragment_date_span / window_days)`` snapshot passes over the
        fragment list. With ``window_days=1`` over a multi-year vault this
        can produce thousands of snapshots; callers generating reports on
        large corpora should prefer the default weekly windowing.

        Args:
            vault_path: Root of the Obsidian vault.
            period: Either ``"weekly"`` or ``"monthly"``. Controls the
                report filename suffix and frontmatter.
            fragments: Fragments to aggregate. Empty lists still produce a
                valid report.
            report_date: Date stamp used in the filename and ``generated_on``
                frontmatter field. Defaults to :meth:`date.today`, so tests
                can pin it for determinism.

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

        snapshots = self._window_snapshots(fragments)
        transitions = self.detect_transitions(snapshots)
        trend = self.track_dosage_trends(fragments)

        stamp = report_date if report_date is not None else date.today()
        body = self._render_report_body(snapshots, transitions, trend)
        post = frontmatter.Post(
            content=body,
            type="wavelength-report",
            period=period,
            generated_on=stamp.isoformat(),
            window_days=self.window_days,
            flagged_frequencies=list(trend.flagged_frequencies),
            tags=["wavelength", f"wavelength-{period}"],
        )

        note_path = target_dir / f"{stamp.isoformat()}-{period}.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    # ---- generate_weekly_report ----

    def generate_weekly_report(
        self,
        vault_path: Path,
        week_of: date,
        *,
        fragments: list[Fragment] | None = None,
    ) -> Path:
        """Write a descriptive weekly wavelength report for the ISO week of *week_of*.

        The report lives at
        ``05-Wavelength/Phase-Maps/YYYY-WNN-wavelength.md`` where ``YYYY``
        and ``NN`` are the ISO year and (zero-padded) ISO week number
        containing *week_of*. The body has seven sections mandated by
        Issue #43: Phase Summary, Domain Mappings (from ontology Section
        7.1), Mode Distribution, Dosage Balance, Emotional Texture Cloud,
        Notable Fragments, and Transition Watch, followed by a Dataview
        query block scoped to this week's fragments.

        Args:
            vault_path: Root of the Obsidian vault.
            week_of: Any date inside the target ISO week. The report
                window is the Monday-to-Sunday ISO week containing
                *week_of*.
            fragments: Fragments to aggregate. When ``None`` the tracker
                reads ``*.md`` notes of ``type: fragment`` from
                ``<vault_path>/01-Fragments/``.

        Returns:
            Path to the written markdown file.
        """
        resolved = self._resolve_fragments(vault_path, fragments)
        iso_year, iso_week, _ = week_of.isocalendar()
        week_start = _week_start(week_of)
        week_end = week_start + timedelta(days=6)

        target_dir = vault_path / "05-Wavelength" / "Phase-Maps"
        target_dir.mkdir(parents=True, exist_ok=True)

        current = self.analyze_period(resolved, week_start, week_end)
        prior_start = week_start - timedelta(days=7)
        prior_end = week_start - timedelta(days=1)
        prior = self.analyze_period(resolved, prior_start, prior_end)

        body = _render_detailed_body(
            current=current,
            prior=prior,
            fragments_in_period=[
                f for f in resolved if _fragment_in_window(f, week_start, week_end)
            ],
            extras=_render_weekly_dataview(week_start, week_end),
        )

        post = frontmatter.Post(
            content=body,
            type="wavelength_report",
            period="weekly",
            week=iso_week,
            year=iso_year,
            week_start=week_start.isoformat(),
            week_end=week_end.isoformat(),
            tags=["wavelength", "wavelength-weekly"],
        )
        note_path = target_dir / f"{iso_year:04d}-W{iso_week:02d}-wavelength.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    # ---- generate_monthly_report ----

    def generate_monthly_report(
        self,
        vault_path: Path,
        month: date,
        *,
        fragments: list[Fragment] | None = None,
    ) -> Path:
        """Write a descriptive monthly wavelength report for *month*.

        The report lives at
        ``05-Wavelength/Phase-Maps/YYYY-MM-wavelength.md`` where ``YYYY``
        and ``MM`` are the calendar year and (zero-padded) month of
        *month*. The body contains the same seven sections as
        :meth:`generate_weekly_report` plus a text-based week-by-week
        phase progression chart and a Month-over-Month comparison
        against the prior calendar month.

        Args:
            vault_path: Root of the Obsidian vault.
            month: Any date inside the target calendar month. The report
                window is ``[first-of-month, last-of-month]``.
            fragments: Fragments to aggregate. When ``None`` the tracker
                reads ``*.md`` notes of ``type: fragment`` from
                ``<vault_path>/01-Fragments/``.

        Returns:
            Path to the written markdown file.
        """
        resolved = self._resolve_fragments(vault_path, fragments)
        month_start = month.replace(day=1)
        last_day = calendar.monthrange(month.year, month.month)[1]
        month_end = month_start.replace(day=last_day)

        target_dir = vault_path / "05-Wavelength" / "Phase-Maps"
        target_dir.mkdir(parents=True, exist_ok=True)

        current = self.analyze_period(resolved, month_start, month_end)
        prior = self._prior_month_snapshot(resolved, month_start)
        weekly_snapshots = self._iter_weekly_snapshots(
            resolved,
            month_start,
            month_end,
        )

        extras: list[str] = []
        extras.extend(_render_progression_chart(weekly_snapshots))
        extras.extend(_render_month_comparison(current, prior))
        extras.extend(_render_monthly_dataview(month_start, month_end))

        body = _render_detailed_body(
            current=current,
            prior=prior,
            fragments_in_period=[
                f for f in resolved if _fragment_in_window(f, month_start, month_end)
            ],
            extras=extras,
        )
        post = frontmatter.Post(
            content=body,
            type="wavelength_report",
            period="monthly",
            month=month_start.month,
            year=month_start.year,
            month_start=month_start.isoformat(),
            month_end=month_end.isoformat(),
            tags=["wavelength", "wavelength-monthly"],
        )
        note_path = (
            target_dir / f"{month_start.year:04d}-{month_start.month:02d}-wavelength.md"
        )
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def _prior_month_snapshot(
        self,
        fragments: list[Fragment],
        month_start: date,
    ) -> WavelengthSnapshot:
        """Return the snapshot for the calendar month preceding *month_start*."""
        prior_end = month_start - timedelta(days=1)
        prior_start = prior_end.replace(day=1)
        return self.analyze_period(fragments, prior_start, prior_end)

    def _iter_weekly_snapshots(
        self,
        fragments: list[Fragment],
        month_start: date,
        month_end: date,
    ) -> list[WavelengthSnapshot]:
        """Return one snapshot per ISO week intersecting the month window."""
        snapshots: list[WavelengthSnapshot] = []
        cursor = _week_start(month_start)
        while cursor <= month_end:
            week_end = cursor + timedelta(days=6)
            bucket_start = max(cursor, month_start)
            bucket_end = min(week_end, month_end)
            snapshots.append(
                self.analyze_period(fragments, bucket_start, bucket_end),
            )
            cursor = week_end + timedelta(days=1)
        return snapshots

    @staticmethod
    def _resolve_fragments(
        vault_path: Path,
        fragments: list[Fragment] | None,
    ) -> list[Fragment]:
        """Return *fragments* when provided, otherwise load them from the vault."""
        if fragments is not None:
            return list(fragments)
        return _load_vault_fragments(vault_path)

    def _window_snapshots(
        self,
        fragments: list[Fragment],
    ) -> list[WavelengthSnapshot]:
        """Build contiguous ``window_days``-sized snapshots spanning the range.

        Windows are anchored to ISO week starts (Monday) for readability;
        if ``window_days`` is not a multiple of 7 the final window may
        extend past the last fragment, which is harmless because
        :meth:`analyze_period` filters by date.

        Dosage trends computed elsewhere always bucket by 7-day ISO weeks
        regardless of ``window_days`` — see :meth:`track_dosage_trends`.
        """
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


# ---- Detailed-report rendering helpers (Issue #43) ----


def _render_detailed_body(
    *,
    current: WavelengthSnapshot,
    prior: WavelengthSnapshot,
    fragments_in_period: list[Fragment],
    extras: list[str],
) -> str:
    """Render the shared weekly/monthly report body plus *extras* sections."""
    lines: list[str] = []
    lines.extend(_render_phase_summary(current))
    lines.extend(_render_domain_mappings(current.dominant_phase))
    lines.extend(_render_mode_distribution(fragments_in_period))
    lines.extend(_render_dosage_balance(current))
    lines.extend(_render_inline_texture_cloud(current))
    lines.extend(_render_notable_fragments(fragments_in_period, current))
    lines.extend(_render_transition_watch(current, prior))
    lines.extend(extras)
    return "\n".join(lines)


def _render_phase_summary(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the Phase Summary section with ontology description."""
    lines = ["## Phase Summary", ""]
    dominant = snapshot.dominant_phase
    if dominant == Phase.UNCLASSIFIED.value:
        lines.append("_No classified phase in this period._")
        lines.append("")
        return lines
    description = PHASE_DESCRIPTIONS.get(dominant, "")
    lines.append(f"- Dominant phase: **{dominant}**")
    lines.append(f"- Confidence: {snapshot.confidence:.2f}")
    lines.append(f"- Fragments analysed: {snapshot.fragment_count}")
    if description:
        lines.append("")
        lines.append(description)
    lines.append("")
    return lines


def _render_domain_mappings(phase: str) -> list[str]:
    """Render the Domain Mappings section per ontology Section 7.1."""
    lines = ["## Domain Mappings", ""]
    mapping = DOMAIN_MAPPINGS.get(phase)
    if mapping is None:
        lines.append("_No domain mapping available for unclassified phases._")
        lines.append("")
        return lines
    lines.append("| Domain | Value |")
    lines.append("| --- | --- |")
    for key, label in _DOMAIN_LABELS:
        lines.append(f"| {label} | {mapping[key]} |")
    lines.append("")
    return lines


def _render_mode_distribution(fragments: list[Fragment]) -> list[str]:
    """Render the Mode Distribution section with counts per engagement mode."""
    lines = ["## Mode Distribution", ""]
    counter: Counter[str] = Counter()
    for fragment in fragments:
        mode = str(fragment.wavelength.mode)
        if mode and mode != Mode.UNCLASSIFIED.value:
            counter[mode] += 1
    if not counter:
        lines.append("_No classified modes in this period._")
        lines.append("")
        return lines
    for mode_name, count in counter.most_common():
        lines.append(f"- **{mode_name}**: {count}")
    lines.append("")
    return lines


def _render_dosage_balance(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the Dosage Balance section as percentage shares."""
    medicine_pct = round(snapshot.medicine_percent * 100)
    toxic_pct = round(snapshot.toxic_percent * 100)
    return [
        "## Dosage Balance",
        "",
        f"- Medicine: {medicine_pct}%",
        f"- Toxic: {toxic_pct}%",
        "",
    ]


def _render_inline_texture_cloud(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the Emotional Texture Cloud as ``tag(count)`` tokens."""
    lines = ["## Emotional Texture Cloud", ""]
    cloud = snapshot.emotional_texture_cloud
    if not cloud:
        lines.append("_No emotional texture tags in this period._")
        lines.append("")
        return lines
    tokens = " ".join(f"{tag}({count})" for tag, count in cloud)
    lines.append(tokens)
    lines.append("")
    return lines


def _render_notable_fragments(
    fragments: list[Fragment],
    snapshot: WavelengthSnapshot,
) -> list[str]:
    """Render up to ``NOTABLE_FRAGMENT_LIMIT`` fragments that best fit the phase."""
    lines = ["## Notable Fragments", ""]
    ranked = _rank_notable(fragments, snapshot.dominant_phase)
    if not ranked:
        lines.append("_No notable fragments identified in this period._")
        lines.append("")
        return lines
    for fragment in ranked[:NOTABLE_FRAGMENT_LIMIT]:
        lines.append(f"- [[{fragment.id}|{fragment.title}]]")
    lines.append("")
    return lines


def _rank_notable(fragments: list[Fragment], dominant_phase: str) -> list[Fragment]:
    """Return fragments ranked by classification completeness, phase match first."""

    def score(fragment: Fragment) -> tuple[int, int, float]:
        """Score higher for dominant-phase match and richer classification."""
        phase = str(fragment.wavelength.phase)
        mode = str(fragment.wavelength.mode)
        dosage = str(fragment.wavelength.dosage)
        frequency = str(fragment.frequency.primary)
        phase_match = 1 if phase == dominant_phase else 0
        completeness = sum(
            1
            for value, unclassified in (
                (phase, Phase.UNCLASSIFIED.value),
                (mode, Mode.UNCLASSIFIED.value),
                (dosage, Dosage.UNCLASSIFIED.value),
                (frequency, Frequency.UNCLASSIFIED.value),
            )
            if value and value != unclassified
        )
        recency = fragment.created.timestamp()
        return (phase_match, completeness, recency)

    return sorted(fragments, key=score, reverse=True)


def _render_transition_watch(
    current: WavelengthSnapshot,
    prior: WavelengthSnapshot,
) -> list[str]:
    """Render the Transition Watch section with the observed phase shift, if any."""
    lines = ["## Transition Watch", ""]
    cur = current.dominant_phase
    prev = prior.dominant_phase
    unclassified = Phase.UNCLASSIFIED.value
    if cur == unclassified or prev == unclassified:
        lines.append(
            "_No transition observable — prior or current phase unclassified._",
        )
        lines.append("")
        return lines
    if cur == prev:
        lines.append(f"Phase held steady at **{cur}** relative to the prior period.")
        lines.append("")
        return lines
    lines.append(f"Phase shifted from **{prev}** to **{cur}**.")
    next_phase = _next_phase(cur)
    if next_phase is not None:
        lines.append(f"If the cycle continues, the next phase may be **{next_phase}**.")
    lines.append("")
    return lines


def _next_phase(phase: str) -> str | None:
    """Return the phase that follows *phase* in the six-phase cycle, or ``None``."""
    cycle = [
        Phase.RISING.value,
        Phase.PEAKING.value,
        Phase.WITHDRAWAL.value,
        Phase.DIMINISHING.value,
        Phase.BOTTOMING_OUT.value,
        Phase.RESTORATION.value,
    ]
    if phase not in cycle:
        return None
    idx = cycle.index(phase)
    return cycle[(idx + 1) % len(cycle)]


def _render_weekly_dataview(week_start: date, week_end: date) -> list[str]:
    """Render a Dataview block scoped to the fragments in this week."""
    return [
        "## Fragments This Week",
        "",
        "```dataview",
        "TABLE wavelength.phase AS Phase, created AS Created",
        'FROM "01-Fragments"',
        (
            f'WHERE type = "fragment" AND created >= date("{week_start.isoformat()}") '
            f'AND created <= date("{week_end.isoformat()}")'
        ),
        "SORT created DESC",
        "```",
        "",
    ]


def _render_monthly_dataview(month_start: date, month_end: date) -> list[str]:
    """Render a Dataview block scoped to the fragments in this month."""
    return [
        "## Fragments This Month",
        "",
        "```dataview",
        "TABLE wavelength.phase AS Phase, created AS Created",
        'FROM "01-Fragments"',
        (
            f'WHERE type = "fragment" AND created >= date("{month_start.isoformat()}") '
            f'AND created <= date("{month_end.isoformat()}")'
        ),
        "SORT created DESC",
        "```",
        "",
    ]


def _render_progression_chart(
    weekly_snapshots: list[WavelengthSnapshot],
) -> list[str]:
    """Render a text-based week-by-week phase progression chart."""
    lines = ["## Phase Progression", ""]
    if not weekly_snapshots:
        lines.append("_No weekly data available._")
        lines.append("")
        return lines
    max_count = max((s.fragment_count for s in weekly_snapshots), default=0) or 1
    for index, snap in enumerate(weekly_snapshots, start=1):
        blocks = _block_width(snap.fragment_count, max_count)
        bar = "#" * blocks if blocks else "."
        phase_label = snap.dominant_phase
        lines.append(
            f"- Week {index} ({snap.start_date.isoformat()}): "
            f"{bar} {phase_label} ({snap.fragment_count})",
        )
    lines.append("")
    return lines


def _block_width(count: int, max_count: int) -> int:
    """Return the ``#``-block width for *count* scaled by *max_count*."""
    if max_count <= 0 or count <= 0:
        return 0
    scaled = round(count / max_count * PROGRESSION_CHART_MAX_WIDTH)
    return max(1, min(PROGRESSION_CHART_MAX_WIDTH, scaled))


def _render_month_comparison(
    current: WavelengthSnapshot,
    prior: WavelengthSnapshot,
) -> list[str]:
    """Render the Month-over-Month comparison section."""
    lines = ["## Month-over-Month", ""]
    lines.append(
        f"- Previous month dominant phase: **{prior.dominant_phase}** "
        f"({prior.fragment_count} fragments)",
    )
    lines.append(
        f"- Current month dominant phase: **{current.dominant_phase}** "
        f"({current.fragment_count} fragments)",
    )
    cur_med = round(current.medicine_percent * 100)
    prev_med = round(prior.medicine_percent * 100)
    lines.append(f"- Medicine share: {prev_med}% → {cur_med}%")
    cur_tox = round(current.toxic_percent * 100)
    prev_tox = round(prior.toxic_percent * 100)
    lines.append(f"- Toxic share: {prev_tox}% → {cur_tox}%")
    lines.append("")
    return lines


# ---- Fragment loading ----


def _load_vault_fragments(vault_path: Path) -> list[Fragment]:
    """Load every Fragment note under ``<vault_path>/01-Fragments/``.

    Markdown files that cannot be parsed as a valid :class:`Fragment`
    are silently skipped; parse errors are logged at DEBUG level.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        List of Fragments, sorted by their ``created`` timestamp.
    """
    fragments_dir = vault_path / _FRAGMENTS_SUBDIR
    if not fragments_dir.is_dir():
        return []
    results: list[Fragment] = []
    for md_file in sorted(fragments_dir.rglob("*.md")):
        fragment = _parse_fragment_file(md_file)
        if fragment is not None:
            results.append(fragment)
    results.sort(key=lambda f: f.created)
    return results


def _parse_fragment_file(md_file: Path) -> Fragment | None:
    """Parse a single markdown note into a :class:`Fragment`, or return ``None``."""
    try:
        post = frontmatter.load(str(md_file))
    except (OSError, ValueError):
        logger.debug("Skipping unreadable fragment note: %s", md_file)
        return None
    metadata = dict(post.metadata)
    if metadata.get("type") != "fragment":
        return None
    try:
        fragment: Fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return fragment


__all__ = [
    "DEFAULT_ROLLING_WEEKS",
    "DEFAULT_TOXIC_CONSECUTIVE_WEEKS",
    "DEFAULT_TOXIC_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "DOMAIN_MAPPINGS",
    "NOTABLE_FRAGMENT_LIMIT",
    "PHASE_DESCRIPTIONS",
    "PROGRESSION_CHART_MAX_WIDTH",
    "DosageTrend",
    "PhaseTransition",
    "WavelengthSnapshot",
    "WavelengthTracker",
]
