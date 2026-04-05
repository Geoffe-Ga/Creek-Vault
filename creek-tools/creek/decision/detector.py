"""Decision detection and support — detect decisions, generate notes, gather context.

Scans fragments for decision-relevant content using keyword matching,
auto-generates draft Decision notes in the vault, and gathers context
from related threads and praxis items.
"""

import logging
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from creek.models import Decision, DecisionStatus, Fragment, Frequency

logger = logging.getLogger(__name__)

# ---- Decision Signal Keywords ----

DECISION_SIGNALS: list[str] = [
    "should i",
    "trying to decide",
    "weighing options",
    "not sure whether",
    "deciding between",
    "decision",
    "torn between",
    "considering whether",
    "debating whether",
    "need to choose",
    "what if i",
    "pros and cons",
    "trade-off",
    "dilemma",
    "crossroads",
]
"""Keywords that signal decision-relevant content in fragments."""

_DECISION_PATTERN = re.compile(
    "|".join(re.escape(s) for s in DECISION_SIGNALS),
    re.IGNORECASE,
)


# ---- Data Models ----


class DetectedDecision(BaseModel):
    """A decision detected from fragment content.

    Attributes:
        fragment_id: ID of the source fragment.
        fragment_title: Title of the source fragment.
        matched_signals: Decision signal keywords found in the content.
        signal_count: Number of decision signals matched.
        frequency_context: Frequency classification of the source fragment.
        wavelength_phase: Wavelength phase at time of detection.
    """

    fragment_id: str
    fragment_title: str
    matched_signals: list[str] = Field(default_factory=list)
    signal_count: int = 0
    frequency_context: list[str] = Field(default_factory=list)
    wavelength_phase: str = ""


# ---- Main Detector ----


class DecisionDetector:
    """Detect decision-relevant fragments and generate draft Decision notes.

    Scans fragment content for decision signal keywords, creates draft
    Decision notes in the vault, and gathers contextual information
    from related threads and praxis items.

    Attributes:
        vault_path: Path to the Obsidian vault root.
        signal_threshold: Minimum number of signal matches to flag
            a fragment as decision-relevant.
    """

    DEFAULT_SIGNAL_THRESHOLD: int = 1

    def __init__(
        self,
        vault_path: Path,
        signal_threshold: int = DEFAULT_SIGNAL_THRESHOLD,
    ) -> None:
        """Initialize the DecisionDetector.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.
            signal_threshold: Minimum signal matches for detection.
        """
        self.vault_path = vault_path
        self.signal_threshold = signal_threshold

    def detect(
        self,
        fragments: list[Fragment],
        content_map: dict[str, str],
    ) -> list[DetectedDecision]:
        """Scan fragments for decision-relevant content.

        Checks each fragment's content against decision signal keywords
        and returns detected decisions for those meeting the threshold.

        Args:
            fragments: Fragments to scan.
            content_map: Mapping of fragment ID to text content.

        Returns:
            List of DetectedDecision objects for flagged fragments.
        """
        detected: list[DetectedDecision] = []

        for frag in fragments:
            content = content_map.get(frag.id, "")
            combined = f"{frag.title} {content}"
            matches = _DECISION_PATTERN.findall(combined)

            if len(matches) < self.signal_threshold:
                continue

            freq_context: list[str] = []
            if frag.frequency.primary != Frequency.UNCLASSIFIED:
                freq_context.append(str(frag.frequency.primary))
            freq_context.extend(str(f) for f in frag.frequency.secondary)

            detected.append(
                DetectedDecision(
                    fragment_id=frag.id,
                    fragment_title=frag.title,
                    matched_signals=[m.lower() for m in matches],
                    signal_count=len(matches),
                    frequency_context=freq_context,
                    wavelength_phase=str(frag.wavelength.phase),
                )
            )

        logger.info(
            "Detected %d decision-relevant fragments from %d total",
            len(detected),
            len(fragments),
        )
        return detected

    def create_draft_decisions(
        self,
        detected: list[DetectedDecision],
    ) -> list[Decision]:
        """Create draft Decision model instances from detected decisions.

        Generates Decision objects in the SENSING status, populated
        with context from the source fragment.

        Args:
            detected: List of DetectedDecision objects to convert.

        Returns:
            List of draft Decision model instances.
        """
        decisions: list[Decision] = []
        for det in detected:
            freq_values = [
                Frequency(f)
                for f in det.frequency_context
                if f in [e.value for e in Frequency]
            ]
            decision = Decision(
                title=f"Decision: {det.fragment_title}",
                status=DecisionStatus.SENSING,
                opened=date.today(),
                frequency_context=freq_values,
                wavelength_phase_at_opening=det.wavelength_phase,
                relevant_threads=[],
                relevant_praxis=[],
                tags=["auto-detected"],
            )
            decisions.append(decision)

        return decisions

    def generate_decision_notes(
        self,
        decisions: list[Decision],
    ) -> list[Path]:
        """Write draft Decision notes to the vault.

        Generates markdown files in ``08-Decisions/Active/`` for each
        draft decision, with frontmatter and context sections.

        Args:
            decisions: Draft Decision model instances to write.

        Returns:
            List of paths to generated decision note files.
        """
        active_dir = self.vault_path / "08-Decisions" / "Active"
        active_dir.mkdir(parents=True, exist_ok=True)

        generated: list[Path] = []
        for decision in decisions:
            content = self._build_decision_markdown(decision)
            safe_title = re.sub(r"[^\w\s-]", "", decision.title)[:60].strip()
            safe_title = re.sub(r"\s+", "-", safe_title)
            filename = f"{safe_title}-{decision.id}.md"
            note_path = active_dir / filename
            note_path.write_text(content, encoding="utf-8")
            generated.append(note_path)

        logger.info("Generated %d decision notes", len(generated))
        return generated

    def _build_decision_markdown(self, decision: Decision) -> str:
        """Build markdown content for a decision note.

        Args:
            decision: The Decision model to render as markdown.

        Returns:
            Complete markdown string with YAML frontmatter and body.
        """
        freq_list = ", ".join(str(f) for f in decision.frequency_context)
        lines = [
            "---",
            "type: decision",
            f"id: {decision.id}",
            f"status: {decision.status}",
            f"opened: {decision.opened.isoformat()}",
            f"frequency_context: [{freq_list}]",
            f"wavelength_phase: {decision.wavelength_phase_at_opening}",
            "tags: [auto-detected]",
            "---",
            "",
            f"# {decision.title}",
            "",
            "## Status",
            "",
            f"**{decision.status}** — Something needs to be decided.",
            "",
            "## Context",
            "",
            "*Context will be gathered from related threads, past decisions, "
            "and current wavelength phase.*",
            "",
            "### Related Threads",
            "",
            "*To be populated by linking pipeline.*",
            "",
            "### Related Past Decisions",
            "",
            "*To be populated by linking pipeline.*",
            "",
            "### Current Wavelength Phase",
            "",
            f"Phase at opening: **{decision.wavelength_phase_at_opening}**",
            "",
            "## Options",
            "",
            "*Options will emerge during deliberation.*",
            "",
            "## Outcome",
            "",
            "*To be recorded when a commitment is made.*",
            "",
        ]

        return "\n".join(lines)

    def run(
        self,
        fragments: list[Fragment],
        content_map: dict[str, str],
    ) -> list[Path]:
        """Execute the full decision detection pipeline.

        Detects decision-relevant fragments, creates draft decisions,
        and generates notes in the vault.

        Args:
            fragments: All classified fragments.
            content_map: Mapping of fragment ID to text content.

        Returns:
            List of paths to generated decision note files.
        """
        detected = self.detect(fragments, content_map)
        decisions = self.create_draft_decisions(detected)
        return self.generate_decision_notes(decisions)
