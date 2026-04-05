"""Wavelength tracking system — temporal analysis, phase detection, reports.

Analyzes fragment wavelength classifications over time to detect phase
transitions, track dosage trends, and generate periodic wavelength
reports as markdown notes in the vault.
"""

import logging
from collections import Counter
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from creek.models import Dosage, Fragment, Phase, WavelengthObservation

logger = logging.getLogger(__name__)


# ---- Data Models ----


class PhaseTransition(BaseModel):
    """A detected transition between wavelength phases.

    Attributes:
        from_phase: The phase before the transition.
        to_phase: The phase after the transition.
        transition_date: The approximate date the transition occurred.
        fragment_count: Number of fragments in the window that evidence
            the new phase.
    """

    from_phase: str
    to_phase: str
    transition_date: date
    fragment_count: int = 0


class DosageTrend(BaseModel):
    """Dosage trend for a time window.

    Attributes:
        window_start: Start date of the analysis window.
        window_end: End date of the analysis window.
        medicine_count: Fragments classified as medicine dosage.
        toxic_count: Fragments classified as toxic dosage.
        ambiguous_count: Fragments classified as ambiguous dosage.
        trend: Summary direction — 'medicine', 'toxic', or 'balanced'.
    """

    window_start: date
    window_end: date
    medicine_count: int = 0
    toxic_count: int = 0
    ambiguous_count: int = 0
    trend: str = "balanced"


class WavelengthReport(BaseModel):
    """A periodic wavelength report summarizing observations.

    Attributes:
        period_start: Start date of the reporting period.
        period_end: End date of the reporting period.
        dominant_phase: The most common phase during the period.
        phase_counts: Count of fragments per phase.
        transitions: Detected phase transitions during the period.
        dosage_trend: Dosage trend summary for the period.
        observations: WavelengthObservation snapshots generated.
        fragment_count: Total fragments analyzed in the period.
    """

    period_start: date
    period_end: date
    dominant_phase: str = "unclassified"
    phase_counts: dict[str, int] = Field(default_factory=dict)
    transitions: list[PhaseTransition] = Field(default_factory=list)
    dosage_trend: DosageTrend | None = None
    observations: list[WavelengthObservation] = Field(default_factory=list)
    fragment_count: int = 0


# ---- Main Tracker ----


class WavelengthTracker:
    """Track wavelength patterns across fragments over time.

    Analyzes temporal clusters of fragments to detect phase transitions,
    track medicine/toxic dosage trends, and generate periodic reports
    as markdown notes in the vault.

    Attributes:
        vault_path: Path to the Obsidian vault root.
        window_days: Size of the sliding analysis window in days.
    """

    DEFAULT_WINDOW_DAYS: int = 7

    def __init__(
        self,
        vault_path: Path,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        """Initialize the WavelengthTracker.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.
            window_days: Number of days per analysis window (default 7).
        """
        self.vault_path = vault_path
        self.window_days = window_days

    def analyze_phases(
        self,
        fragments: list[Fragment],
    ) -> dict[str, int]:
        """Count phase classifications across fragments.

        Args:
            fragments: Fragments to analyze.

        Returns:
            Dictionary mapping phase name to occurrence count.
        """
        counter: Counter[str] = Counter()
        for frag in fragments:
            phase = frag.wavelength.phase
            if phase and phase != Phase.UNCLASSIFIED:
                counter[str(phase)] += 1
        return dict(counter)

    def detect_transitions(
        self,
        fragments: list[Fragment],
    ) -> list[PhaseTransition]:
        """Detect phase transitions in a chronologically sorted fragment list.

        Splits fragments into windows of ``window_days`` size and compares
        the dominant phase between consecutive windows. A transition is
        recorded when the dominant phase changes.

        Args:
            fragments: Fragments sorted by creation date.

        Returns:
            List of detected PhaseTransition objects.
        """
        if not fragments:
            return []

        sorted_frags = sorted(fragments, key=lambda f: f.created)
        windows = self._split_into_windows(sorted_frags)

        transitions: list[PhaseTransition] = []
        prev_phase: str | None = None

        for window_date, window_frags in windows:
            phase_counts = self.analyze_phases(window_frags)
            if not phase_counts:
                continue

            dominant = max(phase_counts, key=lambda k: phase_counts[k])

            if prev_phase is not None and dominant != prev_phase:
                transitions.append(
                    PhaseTransition(
                        from_phase=prev_phase,
                        to_phase=dominant,
                        transition_date=window_date,
                        fragment_count=phase_counts[dominant],
                    )
                )
            prev_phase = dominant

        logger.info("Detected %d phase transitions", len(transitions))
        return transitions

    def analyze_dosage(
        self,
        fragments: list[Fragment],
        window_start: date,
        window_end: date,
    ) -> DosageTrend:
        """Analyze medicine/toxic dosage trends in a time window.

        Args:
            fragments: Fragments to analyze (will be filtered by date).
            window_start: Start of the analysis window.
            window_end: End of the analysis window.

        Returns:
            A DosageTrend summarizing the dosage distribution.
        """
        counted = self._count_dosages(fragments, window_start, window_end)
        medicine = counted.get(Dosage.MEDICINE, 0)
        toxic = counted.get(Dosage.TOXIC, 0)
        ambiguous = counted.get(Dosage.AMBIGUOUS, 0)

        trend = self._determine_trend(medicine, toxic, ambiguous)

        return DosageTrend(
            window_start=window_start,
            window_end=window_end,
            medicine_count=medicine,
            toxic_count=toxic,
            ambiguous_count=ambiguous,
            trend=trend,
        )

    @staticmethod
    def _count_dosages(
        fragments: list[Fragment],
        window_start: date,
        window_end: date,
    ) -> Counter[str]:
        """Count dosage classifications within a date window.

        Args:
            fragments: Fragments to count.
            window_start: Start of window.
            window_end: End of window.

        Returns:
            Counter mapping dosage values to counts.
        """
        counter: Counter[str] = Counter()
        for frag in fragments:
            frag_date = frag.created.date()
            if window_start <= frag_date <= window_end:
                counter[frag.wavelength.dosage] += 1
        return counter

    @staticmethod
    def _determine_trend(
        medicine: int,
        toxic: int,
        ambiguous: int,
    ) -> str:
        """Determine the overall dosage trend direction.

        Args:
            medicine: Count of medicine-dosage fragments.
            toxic: Count of toxic-dosage fragments.
            ambiguous: Count of ambiguous-dosage fragments.

        Returns:
            Trend string: 'medicine', 'toxic', or 'balanced'.
        """
        if medicine > toxic and medicine > ambiguous:
            return "medicine"
        if toxic > medicine and toxic > ambiguous:
            return "toxic"
        return "balanced"

    def generate_report(
        self,
        fragments: list[Fragment],
        period_start: date,
        period_end: date,
    ) -> WavelengthReport:
        """Generate a wavelength report for a specific period.

        Combines phase analysis, transition detection, and dosage
        tracking into a single WavelengthReport.

        Args:
            fragments: All fragments to consider.
            period_start: Start date of the reporting period.
            period_end: End date of the reporting period.

        Returns:
            A WavelengthReport summarizing the period.
        """
        period_frags = [
            f for f in fragments if period_start <= f.created.date() <= period_end
        ]

        phase_counts = self.analyze_phases(period_frags)
        dominant = "unclassified"
        if phase_counts:
            dominant = max(phase_counts, key=lambda k: phase_counts[k])

        transitions = self.detect_transitions(period_frags)
        dosage = self.analyze_dosage(period_frags, period_start, period_end)

        return WavelengthReport(
            period_start=period_start,
            period_end=period_end,
            dominant_phase=dominant,
            phase_counts=phase_counts,
            transitions=transitions,
            dosage_trend=dosage,
            fragment_count=len(period_frags),
        )

    def generate_report_note(self, report: WavelengthReport) -> Path:
        """Write a wavelength report as a markdown note in the vault.

        Generates a note in ``05-Wavelength/Phase-Maps/`` with the
        report contents formatted as readable markdown.

        Args:
            report: The WavelengthReport to write.

        Returns:
            Path to the generated report note file.
        """
        phase_maps_dir = self.vault_path / "05-Wavelength" / "Phase-Maps"
        phase_maps_dir.mkdir(parents=True, exist_ok=True)

        filename = (
            f"Wavelength-Report-"
            f"{report.period_start.isoformat()}-to-"
            f"{report.period_end.isoformat()}.md"
        )
        note_path = phase_maps_dir / filename
        content = self._build_report_markdown(report)
        note_path.write_text(content, encoding="utf-8")

        logger.info("Wavelength report written to %s", note_path)
        return note_path

    def _build_report_markdown(self, report: WavelengthReport) -> str:
        """Build markdown content for a wavelength report note.

        Args:
            report: The WavelengthReport to render as markdown.

        Returns:
            Complete markdown string with YAML frontmatter and body.
        """
        lines = [
            "---",
            "type: wavelength-report",
            f"period_start: {report.period_start.isoformat()}",
            f"period_end: {report.period_end.isoformat()}",
            f"dominant_phase: {report.dominant_phase}",
            f"fragment_count: {report.fragment_count}",
            "---",
            "",
            f"# Wavelength Report: {report.period_start} to {report.period_end}",
            "",
            "## Phase Distribution",
            "",
        ]

        if report.phase_counts:
            for phase, count in sorted(
                report.phase_counts.items(),
                key=lambda x: x[1],
                reverse=True,
            ):
                lines.append(f"- **{phase}**: {count} fragments")
        else:
            lines.append("*No classified phases in this period.*")

        lines.extend(
            [
                "",
                "## Phase Transitions",
                "",
            ]
        )

        if report.transitions:
            for t in report.transitions:
                lines.append(
                    f"- {t.from_phase} -> {t.to_phase} "
                    f"(~{t.transition_date}, {t.fragment_count} fragments)"
                )
        else:
            lines.append("*No phase transitions detected.*")

        lines.extend(
            [
                "",
                "## Dosage Trend",
                "",
            ]
        )

        if report.dosage_trend:
            d = report.dosage_trend
            lines.extend(
                [
                    f"- Medicine: {d.medicine_count}",
                    f"- Toxic: {d.toxic_count}",
                    f"- Ambiguous: {d.ambiguous_count}",
                    f"- Overall trend: **{d.trend}**",
                ]
            )
        else:
            lines.append("*No dosage data available.*")

        lines.append("")
        return "\n".join(lines)

    def run(
        self,
        fragments: list[Fragment],
        period_start: date,
        period_end: date,
    ) -> Path:
        """Execute the full wavelength tracking pipeline.

        Generates a report for the given period and writes it to
        the vault as a markdown note.

        Args:
            fragments: All classified fragments.
            period_start: Start of the reporting period.
            period_end: End of the reporting period.

        Returns:
            Path to the generated wavelength report note.
        """
        report = self.generate_report(fragments, period_start, period_end)
        return self.generate_report_note(report)

    def _split_into_windows(
        self,
        fragments: list[Fragment],
    ) -> list[tuple[date, list[Fragment]]]:
        """Split chronologically sorted fragments into time windows.

        Args:
            fragments: Fragments sorted by creation date.

        Returns:
            List of (window_start_date, fragments_in_window) tuples.
        """
        if not fragments:
            return []

        windows: list[tuple[date, list[Fragment]]] = []
        window_start = fragments[0].created.date()
        current_window: list[Fragment] = []

        for frag in fragments:
            frag_date = frag.created.date()
            if (frag_date - window_start).days >= self.window_days:
                if current_window:
                    windows.append((window_start, current_window))
                window_start = frag_date
                current_window = [frag]
            else:
                current_window.append(frag)

        if current_window:
            windows.append((window_start, current_window))

        return windows
