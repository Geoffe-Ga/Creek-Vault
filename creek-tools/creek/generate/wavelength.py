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
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.privacy_filter import PrivacyTierOverride, within_ceiling
from creek.hierarchy import LevelPolicy, select_by_policy
from creek.models import Dosage, Fragment, Frequency, Mode, Phase
from creek.time import effective_authored_date

if TYPE_CHECKING:
    from enum import StrEnum
    from pathlib import Path


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


_NOTABLE_FRAGMENT_LIMIT: int = 5
"""Maximum number of Notable Fragments listed per report."""

_MAX_BAR_WIDTH: int = 20
"""Cap for ASCII bar widths in the week-by-week progression chart.

Without a cap a busy month (50+ fragments in one week) renders an
unreadable 50+-character bar. Twenty is enough to compare relative
volumes while staying within terminal/Obsidian-pane widths.
"""

_CONFIDENCE_RANK: dict[str, int] = {
    "conviction": 5,
    "settled": 4,
    "forming": 3,
    "exploring": 2,
    "musing": 1,
}
"""Ordinal rank of voice confidence levels for notable-fragment selection."""

_PHASE_DESCRIPTIONS: dict[str, str] = {
    Phase.RISING.value: (
        "Energy building, ideas forming, momentum gathering. "
        "Abundance begins to create indulgence."
    ),
    Phase.PEAKING.value: (
        "Full expression, maximum creative or spiritual output. Abundance peaks."
    ),
    Phase.WITHDRAWAL.value: (
        "First cracks appear, energy turns inward. Indulgence creates scarcity."
    ),
    Phase.DIMINISHING.value: (
        "Active decline, contraction, things falling apart. "
        "Scarcity begins to create resilience."
    ),
    Phase.BOTTOMING_OUT.value: (
        "Lowest point, maximum contraction, dark-night material. Scarcity peaks."
    ),
    Phase.RESTORATION.value: (
        "Return begins, new energy gathering, re-emergence. "
        "Resilience creates abundance."
    ),
    Phase.UNCLASSIFIED.value: (
        "No dominant phase signal in the period — the wavelength is quiet."
    ),
}
"""One-line phase descriptions adapted from ontology §7.1."""

PHASE_DOMAIN_MAPPINGS: dict[str, dict[str, str]] = {
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
        "meditation": "Waking up",
        "breath": "Inhale begin",
    },
}
"""§7.1 cross-domain mapping per phase: Season, Mood, Spaciousness,
Relation to Others / Self, Buddhist Attachment, Meditation, Breath.

Used by :meth:`WavelengthTracker.generate_weekly_report` and
:meth:`WavelengthTracker.generate_monthly_report` to render the
``Domain Mappings`` block.

The ``unclassified`` phase has no row by design: domain mappings are
ontology assertions about a real phase, and rendering "Season:
unclassified" would invent a mapping that does not exist. Renderers
fall back to a placeholder when the dominant phase is unclassified.
"""

_DOMAIN_LABELS: list[tuple[str, str]] = [
    ("season", "Season"),
    ("mood", "Mood (bipolar frame)"),
    ("spaciousness", "Spaciousness"),
    ("relation_to_others", "Relation to Others"),
    ("relation_to_self", "Relation to Self"),
    ("buddhist_attachment", "Buddhist Attachment"),
    ("meditation", "Meditation"),
    ("breath", "Breath"),
]


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Return a parsed frontmatter post, or ``None`` on parse errors."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        return None


def load_fragments_from_vault(
    vault_path: Path,
    *,
    override: PrivacyTierOverride = PrivacyTierOverride.ALL,
) -> list[Fragment]:
    """Load every classified fragment from ``01-Fragments/`` under *vault_path*.

    Files that fail to parse or that lack the ``type: fragment`` marker
    are silently skipped. Returns an empty list when the folder is
    absent.

    Args:
        vault_path: Root of the Obsidian vault.
        override: Tier ceiling (#968), applied to each file's *raw*
            frontmatter — which this loader already has in hand, and which
            is the only place a missing ``privacy_tier`` is still
            distinguishable from the model's ``unclassified`` default.
            Defaults to
            :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
            meaning "no ceiling declared", so the three non-report callers
            in this module and ``creek report --type wavelength`` are
            byte-identical to before. The default is safe because both
            production report surfaces state an override explicitly, which
            ``tests/test_mcp_report_tier_ceiling.py``'s
            ``test_production_report_callers_always_state_an_override``
            enforces structurally.

    Returns:
        Every admitted fragment, in sorted on-disk order.
    """
    root = vault_path / "01-Fragments"
    if not root.exists():
        return []
    fragments: list[Fragment] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "fragment":
            continue
        if not within_ceiling(metadata, override):
            continue
        try:
            fragments.append(Fragment.model_validate(metadata))
        except ValidationError:
            continue
    return fragments


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


def _fragment_effective_date(fragment: Fragment) -> date:
    """Return the date this fragment should be bucketed under (FEAT-031).

    Thin wrapper around :func:`creek.time.effective_authored_date` —
    the single source of truth for the authored-date precedence
    contract. Kept here as a module-private alias so existing imports
    from :mod:`creek.generate.wavelength` keep working; new callers
    should import the canonical helper directly.
    """
    return effective_authored_date(fragment)


def _fragment_in_window(fragment: Fragment, start: date, end: date) -> bool:
    """Return whether *fragment* falls within ``[start, end]`` inclusive.

    Bucketing uses :func:`_fragment_effective_date` so a historical
    export does not register as current-week activity.
    """
    frag_date = _fragment_effective_date(fragment)
    return start <= frag_date <= end


def _most_common_classified(values: list[str], unclassified: StrEnum | str) -> str:
    """Return the most common *values* entry, ignoring *unclassified* markers.

    The sentinel is normalised to a plain :class:`str` before it is
    returned. Callers may legitimately hand over a ``StrEnum`` member
    (``Phase.UNCLASSIFIED``) or its ``.value``: the member satisfies a
    ``str`` annotation, compares equal to its value, and so passes mypy
    and every comparison here unnoticed — but PyYAML has no representer
    for the member, so returning it unchanged made the note it landed in
    fail to serialise with ``RepresenterError`` (issue #940). Normalising
    at the single point of return keeps that trap disarmed for every
    caller rather than relying on each one remembering ``.value``.

    Args:
        values: Candidate markers, typically one per fragment.
        unclassified: The marker treated as "not classified", returned
            (as a plain ``str``) when nothing else qualifies.

    Returns:
        The most frequent classified marker, or the normalised sentinel.
    """
    classified = [v for v in values if v and v != unclassified]
    if not classified:
        return str(unclassified)
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
        return dict(grouped)  # noqa: FURB123  # converts defaultdict to plain dict so callers cannot accidentally insert.

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
            week = _week_start(_fragment_effective_date(fragment))
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
            flagged_frequencies=trend.flagged_frequencies.copy(),
            tags=["wavelength", f"wavelength-{period}"],
        )

        note_path = target_dir / f"{stamp.isoformat()}-{period}.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

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
        # FEAT-031: snapshot windows anchor on effective authored dates
        # so re-ingested historical material does not pull a series'
        # start/end forward to the import moment.
        first = min(effective_authored_date(f) for f in fragments)
        last = max(effective_authored_date(f) for f in fragments)
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

    def generate_weekly_report(
        self,
        vault_path: Path,
        week_of: date,
        fragments: list[Fragment] | None = None,
    ) -> Path:
        """Write a §7.1-shaped weekly wavelength report to ``Phase-Maps/``.

        The report covers the ISO week containing *week_of*. Sections:
        Phase Summary, Domain Mappings, Mode Distribution, Dosage
        Balance, Emotional Texture Cloud, Notable Fragments, Transition
        Watch. Language is descriptive only — never prescriptive.

        Args:
            vault_path: Vault root.
            week_of: Any date within the target ISO week.
            fragments: Optional pre-loaded fragments. When ``None`` the
                method scans ``vault_path/01-Fragments`` and loads all
                classified fragments itself.

        Returns:
            Path to the written ``YYYY-WNN-wavelength.md`` file.
        """
        loaded = (
            fragments
            if fragments is not None
            else load_fragments_from_vault(vault_path)
        )
        start = _week_start(week_of)
        end = start + timedelta(days=6)
        in_window = [f for f in loaded if _fragment_in_window(f, start, end)]
        snapshot = self.analyze_period(in_window, start, end)
        transitions = self._weekly_transition_watch(loaded, start)
        trend = self.track_dosage_trends(loaded)

        iso_year, iso_week, _ = week_of.isocalendar()
        body = _render_weekly_body(
            snapshot,
            in_window,
            trend,
            transitions,
        )
        post = frontmatter.Post(
            content=body,
            type="wavelength_report",
            period="weekly",
            week=iso_week,
            year=iso_year,
            generated_on=date.today().isoformat(),
            tags=["wavelength", "wavelength-weekly"],
        )
        target_dir = vault_path / "05-Wavelength" / "Phase-Maps"
        target_dir.mkdir(parents=True, exist_ok=True)
        note_path = target_dir / f"{iso_year}-W{iso_week:02d}-wavelength.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def generate_monthly_report(
        self,
        vault_path: Path,
        month: date,
        fragments: list[Fragment] | None = None,
    ) -> Path:
        """Write a §7.1-shaped monthly wavelength report to ``Phase-Maps/``.

        The report aggregates the calendar month containing *month*.
        Same sections as :meth:`generate_weekly_report` plus a
        week-by-week ASCII progression chart and a month-over-month
        comparison.

        Args:
            vault_path: Vault root.
            month: Any date within the target month.
            fragments: Optional pre-loaded fragments. When ``None`` the
                method scans ``vault_path/01-Fragments``.

        Returns:
            Path to the written ``YYYY-MM-wavelength.md`` file.
        """
        loaded = (
            fragments
            if fragments is not None
            else load_fragments_from_vault(vault_path)
        )
        start, end = _month_bounds(month)
        in_window = [f for f in loaded if _fragment_in_window(f, start, end)]
        snapshot = self.analyze_period(in_window, start, end)
        weekly_snapshots = self.weekly_snapshots(in_window, start, end)
        prev_start, prev_end = _month_bounds(start - timedelta(days=1))
        prev_window = [
            f for f in loaded if _fragment_in_window(f, prev_start, prev_end)
        ]
        prev_snapshot = self.analyze_period(prev_window, prev_start, prev_end)
        transitions = self.detect_transitions(weekly_snapshots)
        trend = self.track_dosage_trends(loaded)

        body = _render_monthly_body(
            snapshot,
            weekly_snapshots,
            prev_snapshot,
            in_window,
            trend,
            transitions,
        )
        post = frontmatter.Post(
            content=body,
            type="wavelength_report",
            period="monthly",
            month=month.month,
            year=month.year,
            generated_on=date.today().isoformat(),
            tags=["wavelength", "wavelength-monthly"],
        )
        target_dir = vault_path / "05-Wavelength" / "Phase-Maps"
        target_dir.mkdir(parents=True, exist_ok=True)
        note_path = target_dir / f"{month.year}-{month.month:02d}-wavelength.md"
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def weekly_snapshots(
        self,
        fragments: list[Fragment],
        start: date,
        end: date,
    ) -> list[WavelengthSnapshot]:
        """Return one snapshot per ISO week between *start* and *end*.

        Public since FEAT-007: ``current_phase_summary`` (and any future
        caller that needs week-bucketed snapshots without the report
        rendering) calls it. Earlier versions kept it private when the
        only call site was :meth:`generate_monthly_report`.
        """
        snapshots: list[WavelengthSnapshot] = []
        cursor = _week_start(start)
        while cursor <= end:
            window_end = min(cursor + timedelta(days=6), end)
            window = [
                f for f in fragments if _fragment_in_window(f, cursor, window_end)
            ]
            snapshots.append(self.analyze_period(window, cursor, window_end))
            cursor = window_end + timedelta(days=1)
        return snapshots

    def _weekly_transition_watch(
        self,
        fragments: list[Fragment],
        target_week_start: date,
    ) -> list[PhaseTransition]:
        """Return any transition between the previous and target ISO week.

        Args:
            fragments: The full vault fragment set, not just the target
                week. Transition detection needs the prior week's
                fragments to compare against, so callers must pass the
                unfiltered list.
            target_week_start: Monday of the target ISO week.
        """
        prev_start = target_week_start - timedelta(days=7)
        prev_end = target_week_start - timedelta(days=1)
        target_end = target_week_start + timedelta(days=6)
        prev_window = [
            f for f in fragments if _fragment_in_window(f, prev_start, prev_end)
        ]
        target_window = [
            f
            for f in fragments
            if _fragment_in_window(f, target_week_start, target_end)
        ]
        snapshots = [
            self.analyze_period(prev_window, prev_start, prev_end),
            self.analyze_period(target_window, target_week_start, target_end),
        ]
        return self.detect_transitions(snapshots)


def _month_bounds(day: date) -> tuple[date, date]:
    """Return ``(first_of_month, last_of_month)`` for the month of *day*."""
    start = day.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    end = next_month - timedelta(days=1)
    return start, end


# ---- Report rendering helpers ----


def _render_current_phase(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render the ``Current Phase`` section describing the latest snapshot."""
    lines = ["## Current Phase", ""]
    if not snapshots:
        lines.extend(("_No fragments available for this period._", ""))
        return lines
    current = snapshots[-1]
    lines.extend(
        (
            f"- Dominant phase: **{current.dominant_phase}** "
            f"(confidence {current.confidence:.2f})",
            f"- Dominant mode: **{current.dominant_mode}**",
            f"- Fragment count: {current.fragment_count}",
            f"- Medicine share: {current.medicine_percent:.2f} | "
            f"Toxic share: {current.toxic_percent:.2f}",
            "",
        )
    )
    return lines


def _render_phase_history(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render one bullet per snapshot as a chronological history."""
    lines = ["## Phase History", ""]
    if not snapshots:
        lines.extend(("_No snapshots recorded._", ""))
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
        lines.extend(("_No classified frequencies recorded._", ""))
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
        lines.extend(
            (
                "",
                "Frequencies trending toward overdose (descriptive signal):",
            )
        )
        for freq in sorted(trend.flagged_frequencies):
            lines.append(f"- {freq}")
    lines.append("")
    return lines


def _render_transition_log(transitions: list[PhaseTransition]) -> list[str]:
    """Render one bullet per detected phase transition."""
    lines = ["## Transition Log", ""]
    if not transitions:
        lines.extend(("_No phase transitions detected in this period._", ""))
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
        lines.extend(("_No emotional texture tags recorded._", ""))
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


# ---- Weekly and monthly section renderers ----


def _render_phase_summary(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the ``Phase Summary`` section with the §7.1 description."""
    description = _PHASE_DESCRIPTIONS.get(
        snapshot.dominant_phase,
        _PHASE_DESCRIPTIONS[Phase.UNCLASSIFIED.value],
    )
    return [
        "## Phase Summary",
        "",
        f"- Dominant phase: **{snapshot.dominant_phase}**",
        f"- Classification confidence: {snapshot.confidence:.2f}",
        f"- Fragments observed: {snapshot.fragment_count}",
        f"- {description}",
        "",
    ]


def _render_domain_mappings(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the §7.1 cross-domain mappings for the dominant phase."""
    lines = ["## Domain Mappings", ""]
    mapping = PHASE_DOMAIN_MAPPINGS.get(snapshot.dominant_phase)
    if mapping is None:
        lines.extend(("_No mapping available — dominant phase is unclassified._", ""))
        return lines
    for key, label in _DOMAIN_LABELS:
        lines.append(f"- **{label}**: {mapping[key]}")
    lines.append("")
    return lines


def _render_mode_distribution(fragments: list[Fragment]) -> list[str]:
    """Render the ``Mode Distribution`` section as a counted list."""
    lines = ["## Mode Distribution", ""]
    counts: Counter[str] = Counter(
        str(f.wavelength.mode)
        for f in fragments
        if str(f.wavelength.mode) and str(f.wavelength.mode) != Mode.UNCLASSIFIED.value
    )
    if not counts:
        lines.extend(("_No classified modes in this period._", ""))
        return lines
    for mode, count in counts.most_common():
        lines.append(f"- `{mode}`: {count}")
    lines.append("")
    return lines


def _render_dosage_balance(snapshot: WavelengthSnapshot) -> list[str]:
    """Render the ``Dosage Balance`` section with medicine vs toxic shares."""
    return [
        "## Dosage Balance",
        "",
        f"- Medicine share: {snapshot.medicine_percent * 100:.1f}%",
        f"- Toxic share: {snapshot.toxic_percent * 100:.1f}%",
        "",
    ]


def _render_emotional_texture_cloud(
    snapshots: list[WavelengthSnapshot],
) -> list[str]:
    """Render the ``Emotional Texture Cloud`` aggregated across snapshots."""
    lines = ["## Emotional Texture Cloud", ""]
    merged: Counter[str] = Counter()
    for snap in snapshots:
        for tag, count in snap.emotional_texture_cloud:
            merged[tag] += count
    if not merged:
        lines.extend(("_No emotional texture tags recorded._", ""))
        return lines
    cloud = " ".join(f"{tag}({count})" for tag, count in merged.most_common())
    lines.extend((cloud, ""))
    return lines


def _render_notable_fragments(fragments: list[Fragment]) -> list[str]:
    """Render up to ``_NOTABLE_FRAGMENT_LIMIT`` highest-confidence fragments."""
    lines = ["## Notable Fragments", ""]
    if not fragments:
        lines.extend(("_No fragments observed in this period._", ""))
        return lines
    ranked = sorted(
        fragments,
        key=_notable_fragment_score,
        reverse=True,
    )
    for fragment in ranked[:_NOTABLE_FRAGMENT_LIMIT]:
        lines.append(
            f"- [[{fragment.id}]] — *{fragment.title}* "
            f"(phase: {fragment.wavelength.phase})",
        )
    lines.append("")
    return lines


def _notable_fragment_score(fragment: Fragment) -> tuple[int, str]:
    """Score a fragment for notable-selection: confidence rank, then ID."""
    confidence = fragment.voice.confidence
    rank = _CONFIDENCE_RANK.get(str(confidence), 0) if confidence is not None else 0
    return (rank, fragment.id)


def _render_transition_watch(
    snapshot: WavelengthSnapshot,
    transitions: list[PhaseTransition],
) -> list[str]:
    """Render the ``Transition Watch`` section in descriptive language."""
    lines = ["## Transition Watch", ""]
    if transitions:
        for trans in transitions:
            lines.append(
                f"- A shift from `{trans.from_phase}` to `{trans.to_phase}` "
                f"appears between {trans.from_date.isoformat()} and "
                f"{trans.to_date.isoformat()}.",
            )
    else:
        lines.append(
            f"- No phase transition appears in this period; the "
            f"`{snapshot.dominant_phase}` reading is steady.",
        )
    lines.append("")
    return lines


def _render_week_by_week_chart(snapshots: list[WavelengthSnapshot]) -> list[str]:
    """Render the ASCII week-by-week phase progression chart.

    Bar width is capped at :data:`_MAX_BAR_WIDTH` characters so a busy
    week does not blow out the chart. Zero-fragment weeks render
    ``(empty)`` rather than a stray ``#`` so "no activity" is visually
    distinct from "one fragment".
    """
    lines = ["## Week-by-Week Progression", ""]
    if not snapshots:
        lines.extend(("_No weekly snapshots in this month._", ""))
        return lines
    for index, snap in enumerate(snapshots, start=1):
        if snap.fragment_count == 0:
            bar = "(empty)"
        else:
            bar = "#" * min(snap.fragment_count, _MAX_BAR_WIDTH)
        lines.append(
            f"- Week {index:02d} ({snap.start_date.isoformat()}): "
            f"{bar} {snap.dominant_phase} "
            f"({snap.fragment_count} fragments)",
        )
    lines.append("")
    return lines


def _render_month_over_month(
    current: WavelengthSnapshot,
    previous: WavelengthSnapshot,
) -> list[str]:
    """Render the ``Month-over-Month`` comparison block."""
    lines = ["## Month-over-Month", ""]
    if previous.fragment_count == 0:
        lines.extend(("_No prior month data to compare against._", ""))
        return lines
    delta = current.fragment_count - previous.fragment_count
    direction = (
        "more" if delta > 0 else ("fewer" if delta < 0 else "the same number of")
    )
    lines.extend(
        (
            f"- Previous month dominant phase: `{previous.dominant_phase}` "
            f"({previous.fragment_count} fragments)",
            f"- This month dominant phase: `{current.dominant_phase}` "
            f"({current.fragment_count} fragments)",
            f"- Volume change: {direction} fragments ({delta:+d}).",
            "",
        )
    )
    return lines


def _render_weekly_body(
    snapshot: WavelengthSnapshot,
    fragments: list[Fragment],
    trend: DosageTrend,
    transitions: list[PhaseTransition],
) -> str:
    """Compose the markdown body for a weekly report."""
    lines: list[str] = []
    lines.extend(_render_phase_summary(snapshot))
    lines.extend(_render_domain_mappings(snapshot))
    lines.extend(_render_mode_distribution(fragments))
    lines.extend(_render_dosage_balance(snapshot))
    lines.extend(_render_emotional_texture_cloud([snapshot]))
    lines.extend(_render_notable_fragments(fragments))
    lines.extend(_render_transition_watch(snapshot, transitions))
    lines.extend(_render_dosage_trends(trend))
    lines.extend(_render_dataview_section())
    return "\n".join(lines)


def _render_monthly_body(
    snapshot: WavelengthSnapshot,
    weekly_snapshots: list[WavelengthSnapshot],
    previous_month: WavelengthSnapshot,
    fragments: list[Fragment],
    trend: DosageTrend,
    transitions: list[PhaseTransition],
) -> str:
    """Compose the markdown body for a monthly report."""
    lines: list[str] = []
    lines.extend(_render_phase_summary(snapshot))
    lines.extend(_render_domain_mappings(snapshot))
    lines.extend(_render_mode_distribution(fragments))
    lines.extend(_render_dosage_balance(snapshot))
    lines.extend(_render_emotional_texture_cloud(weekly_snapshots))
    lines.extend(_render_notable_fragments(fragments))
    lines.extend(_render_transition_watch(snapshot, transitions))
    lines.extend(_render_week_by_week_chart(weekly_snapshots))
    lines.extend(_render_month_over_month(snapshot, previous_month))
    lines.extend(_render_dosage_trends(trend))
    lines.extend(_render_dataview_section())
    return "\n".join(lines)


DEFAULT_CURRENT_PHASE_WINDOW_DAYS: int = 28
"""Window (days) used by :func:`current_phase_summary` for stable phase reads."""


@dataclass(frozen=True)
class CurrentPhaseSummary:
    """Compact descriptive view of the vault's current wavelength state.

    Used by ``creek state`` (FEAT-007) to render the wavelength snapshot
    that primes the rest of the audit report. The summary is descriptive
    only — no prescription, no normative phase ranking.

    Attributes:
        phase: Dominant phase value within the analysis window, or
            :attr:`Phase.UNCLASSIFIED` when no classified fragments exist.
        mode: Dominant engagement mode value.
        medicine_percent: Share of classified fragments marked ``medicine``.
        toxic_percent: Share of classified fragments marked ``toxic``.
        transitions: Phase transitions detected between adjacent week
            buckets within the analysis window, oldest first.
        fragment_count: Number of fragments that fell inside the window.
        confidence: Share of classified phases that match :attr:`phase`,
            in ``[0.0, 1.0]``.
    """

    phase: str
    mode: str
    medicine_percent: float
    toxic_percent: float
    transitions: tuple[PhaseTransition, ...]
    fragment_count: int
    confidence: float


def current_phase_summary(
    vault_path: Path,
    *,
    today: date | None = None,
    window_days: int = DEFAULT_CURRENT_PHASE_WINDOW_DAYS,
    level_policy: LevelPolicy = "all",
) -> CurrentPhaseSummary:
    """Return a :class:`CurrentPhaseSummary` for the trailing *window_days*.

    Reads classified fragments under ``01-Fragments``, aggregates them
    into one whole-window snapshot, and detects phase transitions between
    consecutive ISO-weeks within the window. When the vault has no
    classified fragments the summary degrades gracefully to an
    unclassified phase with zero counts.

    Args:
        vault_path: Root of the Obsidian vault.
        today: Optional pinned date for tests. Defaults to today (UTC),
            matching the convention used by :class:`StateReportGenerator`
            and the rest of the audit-report pipeline. ``date.today()``
            (system-local) would silently shift the 28-day window by a
            day near midnight UTC on a non-UTC host.
        window_days: Lookback (inclusive). Defaults to 28 days — long
            enough that one quiet week does not flip the dominant phase.
        level_policy: FEAT-025 level filter applied before aggregation.
            ``"all"`` (default) reproduces the pre-FEAT-025 behaviour.
            ``creek state`` passes ``"documents"`` so the snapshot is
            read at whole-source granularity.

    Returns:
        A :class:`CurrentPhaseSummary`.
    """
    anchor = today or datetime.now(tz=UTC).date()
    start = anchor - timedelta(days=window_days - 1)
    fragments = load_fragments_from_vault(vault_path)
    fragments = select_by_policy(fragments, level_policy)
    tracker = WavelengthTracker()
    snapshot = tracker.analyze_period(fragments, start, anchor)
    weekly = tracker.weekly_snapshots(fragments, start, anchor)
    transitions = tracker.detect_transitions(weekly)
    return CurrentPhaseSummary(
        phase=snapshot.dominant_phase,
        mode=snapshot.dominant_mode,
        medicine_percent=snapshot.medicine_percent,
        toxic_percent=snapshot.toxic_percent,
        transitions=tuple(transitions),
        fragment_count=snapshot.fragment_count,
        confidence=snapshot.confidence,
    )


_MODE_PROFILE_SUBPATH: tuple[str, str] = ("05-Wavelength", "Mode-Profiles")
"""Vault subdirectory where per-mode profile notes are persisted (#583)."""

_MODE_PROFILE_SAMPLE_LIMIT: int = 5
"""Maximum sample fragments listed in a mode profile note."""


class ModeProfileGenerator:
    """Write per-mode engagement profiles to ``05-Wavelength/Mode-Profiles/``.

    Aggregates the corpus by engagement :class:`~creek.models.Mode` — modes are
    derived from the enum, never hardcoded — and writes one note per mode that
    has at least one fragment, summarising its fragment count and dominant
    frequency / phase. Modes with no fragments are skipped (no empty notes), so
    an unclassified corpus produces no files at all (#583).
    """

    def generate_mode_profiles(
        self,
        vault_path: Path,
        *,
        override: PrivacyTierOverride = PrivacyTierOverride.ALL,
    ) -> list[Path]:
        """Write one profile note per non-empty engagement mode.

        Args:
            vault_path: Path to the root of the Obsidian vault.
            override: Tier ceiling (#968), forwarded to
                :func:`load_fragments_from_vault`. Defaults to
                :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
                meaning "no ceiling declared" — a genuine no-op for callers
                that predate #968, and safe because both production report
                surfaces state an override explicitly (pinned by
                ``tests/test_mcp_report_tier_ceiling.py``'s
                ``test_production_report_callers_always_state_an_override``).
                A mode profile lists sample fragment *titles*, so an
                unfiltered walk publishes above-ceiling titles into
                ``05-Wavelength/Mode-Profiles/``.

        Returns:
            Paths written, in canonical :class:`~creek.models.Mode` order; empty
            when no fragment carries a classified mode.
        """
        # ``WavelengthClassification`` sets ``use_enum_values=True``, so
        # ``fragment.wavelength.mode`` is already the canonical slug *string*
        # (not a ``Mode`` member). Key the buckets by that string and iterate the
        # enum's ``.value``s below, so the comparison stays string-to-string
        # throughout rather than mixing enum identity with string lookups.
        by_mode: dict[str, list[Fragment]] = defaultdict(list)
        for fragment in load_fragments_from_vault(vault_path, override=override):
            mode = str(fragment.wavelength.mode)
            if mode != Mode.UNCLASSIFIED.value:
                by_mode[mode].append(fragment)
        written: list[Path] = []
        for mode in Mode:
            if mode is Mode.UNCLASSIFIED:
                continue
            fragments = by_mode.get(mode.value)
            if fragments:
                written.append(
                    self._write_mode_profile(mode.value, fragments, vault_path),
                )
        return written

    @staticmethod
    def _write_mode_profile(
        mode: str,
        fragments: list[Fragment],
        vault_path: Path,
    ) -> Path:
        """Render and write a single mode's profile note; return its path."""
        target_dir = vault_path.joinpath(*_MODE_PROFILE_SUBPATH)
        target_dir.mkdir(parents=True, exist_ok=True)
        dominant_frequency = _most_common_classified(
            [str(f.frequency.primary) for f in fragments],
            Frequency.UNCLASSIFIED,
        )
        dominant_phase = _most_common_classified(
            [str(f.wavelength.phase) for f in fragments],
            Phase.UNCLASSIFIED,
        )
        lines = [
            f"# Mode Profile — {mode}",
            "",
            f"- Fragments in this mode: {len(fragments)}",
            f"- Dominant frequency: {dominant_frequency}",
            f"- Dominant phase: {dominant_phase}",
            "",
            "## Sample Fragments",
            "",
            *(f"- {f.title} ({f.id})" for f in fragments[:_MODE_PROFILE_SAMPLE_LIMIT]),
            "",
        ]
        post = frontmatter.Post(
            content="\n".join(lines),
            type="mode_profile",
            mode=mode,
            fragment_count=len(fragments),
            dominant_frequency=dominant_frequency,
            dominant_phase=dominant_phase,
            generated_date=datetime.now(tz=UTC).isoformat(),
            tags=["wavelength", "mode-profile", mode],
        )
        target = target_dir / f"{mode}.md"
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target


__all__ = [
    "DEFAULT_CURRENT_PHASE_WINDOW_DAYS",
    "DEFAULT_ROLLING_WEEKS",
    "DEFAULT_TOXIC_CONSECUTIVE_WEEKS",
    "DEFAULT_TOXIC_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "PHASE_DOMAIN_MAPPINGS",
    "CurrentPhaseSummary",
    "DosageTrend",
    "ModeProfileGenerator",
    "PhaseTransition",
    "WavelengthSnapshot",
    "WavelengthTracker",
    "current_phase_summary",
    "load_fragments_from_vault",
]
