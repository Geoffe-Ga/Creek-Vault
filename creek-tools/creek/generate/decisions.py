"""Decision detection — flag decision-relevant fragments.

Detects decision-relevant content in Creek fragments using keyword matching
and frequency/confidence pattern analysis, generates draft Decision notes
in the vault, and manages decision phase transitions between Active and
Archive folders.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

import frontmatter

from creek.models import (
    DecisionCandidate,
    DecisionStatus,
    Frequency,
    PraxisPotential,
    _generate_decision_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment

DECISION_KEYWORDS: tuple[str, ...] = (
    "should i",
    "trying to decide",
    "weighing options",
    "not sure whether",
    "torn between",
    "considering",
    "the question is",
)
"""Case-insensitive keywords that signal decision-relevant content."""

# Frequency pairs that indicate active deliberation when combined
# with explicit praxis potential and exploring confidence.
_DECISION_FREQUENCY_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        (Frequency.F1, Frequency.F4),
        (Frequency.F1, Frequency.F5),
    },
)

# Statuses that map to the Active/ folder; all others go to Archive/.
_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        DecisionStatus.SENSING,
        DecisionStatus.DELIBERATING,
        DecisionStatus.COMMITTING,
    },
)

_VALID_PHASES: frozenset[str] = frozenset(
    {
        DecisionStatus.SENSING,
        DecisionStatus.DELIBERATING,
        DecisionStatus.COMMITTING,
        DecisionStatus.ENACTED,
        DecisionStatus.REFLECTING,
    },
)


def _sanitize_title(title: str) -> str:
    """Sanitise a title string into a safe filename component.

    Args:
        title: The raw title string.

    Returns:
        A sanitised string suitable for use in a filename.
    """
    cleaned = re.sub(r"[^\w\s-]", "", title)
    cleaned = cleaned.strip().replace(" ", "-")
    return cleaned[:80]


def _build_frequency_context(fragment: Fragment) -> list[str]:
    """Extract all frequency values from a fragment.

    Args:
        fragment: The source fragment.

    Returns:
        List of frequency code strings (e.g. ``["F1", "F5"]``).
    """
    freqs: list[str] = []
    primary = str(fragment.frequency.primary)
    if primary != "unclassified":
        freqs.append(primary)
    for sec in fragment.frequency.secondary:
        freqs.append(str(sec))
    return freqs


class DecisionDetector:
    """Detect decision-relevant fragments and manage decision notes.

    Provides three capabilities: scanning fragments for decision signals
    (keywords and frequency/confidence patterns), creating draft Decision
    notes in the vault, and updating decision phase with folder moves.
    """

    def detect_decisions(
        self,
        fragments: list[Fragment],
    ) -> list[DecisionCandidate]:
        """Scan fragments for decision-relevant signals.

        Uses two detection strategies:

        1. **Keyword detection** — case-insensitive matching of known
           decision phrases in the fragment title.
        2. **Pattern detection** — high frequency overlap between F1
           (Agency) and F4 (Structure) or F5 (Achievement), combined
           with ``praxis_potential = "explicit"`` and
           ``confidence = "exploring"``.

        A fragment matching both strategies produces a single candidate.

        Args:
            fragments: List of Fragment models to scan.

        Returns:
            List of DecisionCandidate models for flagged fragments.
        """
        candidates: list[DecisionCandidate] = []
        for fragment in fragments:
            keyword_hits = self._detect_keywords(fragment)
            pattern_hit = self._detect_pattern(fragment)

            if not keyword_hits and not pattern_hit:
                continue

            methods: list[str] = []
            if keyword_hits:
                methods.append("keyword")
            if pattern_hit:
                methods.append("pattern")

            # Score: keywords=0.7 base, pattern=0.6 base, both=0.9
            score = 0.0
            if keyword_hits and pattern_hit:
                score = 0.9
            elif keyword_hits:
                score = 0.7
            else:
                score = 0.6

            candidate = DecisionCandidate(
                fragment_id=fragment.id,
                fragment_title=fragment.title,
                matched_keywords=keyword_hits,
                detection_method="+".join(methods),
                confidence_score=score,
                wavelength_phase_at_detection=str(fragment.wavelength.phase),
                frequency_context=_build_frequency_context(fragment),
            )
            candidates.append(candidate)

        return candidates

    def create_decision_note(
        self,
        candidate: DecisionCandidate,
        vault_path: Path,
    ) -> Path:
        """Create a draft Decision note in the vault from a candidate.

        Generates a markdown file with YAML frontmatter in
        ``08-Decisions/Active/``, defaulting to ``sensing`` status.

        Args:
            candidate: The DecisionCandidate to create a note for.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the created decision note file.
        """
        decision_id = _generate_decision_id()
        today = date.today()

        body = f"## Source Fragments\n\n- {candidate.fragment_id}\n\n"
        body += "## Options\n\n- _Option 1_\n- _Option 2_\n\n"
        body += "## Criteria\n\n- _Add evaluation criteria here_\n"

        post = frontmatter.Post(
            content=body,
            type="decision",
            id=decision_id,
            title=candidate.fragment_title,
            status=str(DecisionStatus.SENSING),
            opened=today.isoformat(),
            wavelength_phase_at_opening=candidate.wavelength_phase_at_detection,
            frequency_context=list(candidate.frequency_context),
            detection_method=candidate.detection_method,
            confidence_score=candidate.confidence_score,
        )

        target_dir = vault_path / "08-Decisions" / "Active"
        target_dir.mkdir(parents=True, exist_ok=True)

        sanitized = _sanitize_title(candidate.fragment_title)
        filename = (
            f"{today.isoformat()}-{sanitized}.md"
            if sanitized
            else f"{today.isoformat()}.md"
        )
        note_path = target_dir / filename

        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def update_decision_phase(
        self,
        decision_id: str,
        new_phase: str,
        vault_path: Path,
    ) -> Path:
        """Update a decision's phase and move between Active/Archive folders.

        Active statuses (sensing, deliberating, committing) live in
        ``08-Decisions/Active/``. Completed statuses (enacted, reflecting)
        live in ``08-Decisions/Archive/``.

        Args:
            decision_id: The ID of the decision to update.
            new_phase: The new phase string (must be a valid DecisionStatus).
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the (possibly moved) decision note.

        Raises:
            ValueError: If the decision ID is not found or the phase is invalid.
        """
        if new_phase not in _VALID_PHASES:
            msg = f"Invalid decision phase: {new_phase!r}"
            raise ValueError(msg)

        decisions_dir = vault_path / "08-Decisions"
        source_path = self._find_decision_by_id(decision_id, decisions_dir)
        if source_path is None:
            msg = f"Decision {decision_id!r} not found in vault"
            raise ValueError(msg)

        # Update frontmatter
        post = frontmatter.load(str(source_path))
        post["status"] = new_phase
        source_path.write_text(frontmatter.dumps(post), encoding="utf-8")

        # Determine correct folder
        target_subfolder = "Active" if new_phase in _ACTIVE_STATUSES else "Archive"
        target_dir = decisions_dir / target_subfolder
        target_dir.mkdir(parents=True, exist_ok=True)

        # Move if needed
        if source_path.parent != target_dir:
            dest_path = target_dir / source_path.name
            source_path.rename(dest_path)
            return dest_path

        return source_path

    @staticmethod
    def _detect_keywords(fragment: Fragment) -> list[str]:
        """Check fragment title for decision keywords (case-insensitive).

        Args:
            fragment: The fragment to check.

        Returns:
            List of matched keyword strings, empty if none matched.
        """
        title_lower = fragment.title.lower()
        return [kw for kw in DECISION_KEYWORDS if kw in title_lower]

    @staticmethod
    def _detect_pattern(fragment: Fragment) -> bool:
        """Check fragment for frequency/confidence pattern signals.

        A fragment matches the pattern if it has ``praxis_potential = "explicit"``
        AND ``confidence = "exploring"`` AND its primary+secondary frequencies
        contain a known decision-indicating pair (F1+F4 or F1+F5).

        Args:
            fragment: The fragment to check.

        Returns:
            True if the fragment matches the deliberation pattern.
        """
        if str(fragment.praxis_potential) != PraxisPotential.EXPLICIT:
            return False

        voice_confidence = (
            str(fragment.voice.confidence) if fragment.voice.confidence else ""
        )
        if voice_confidence != "exploring":
            return False

        all_freqs: set[str] = {str(fragment.frequency.primary)}
        for sec in fragment.frequency.secondary:
            all_freqs.add(str(sec))

        return any(
            f1 in all_freqs and f2 in all_freqs for f1, f2 in _DECISION_FREQUENCY_PAIRS
        )

    @staticmethod
    def _find_decision_by_id(
        decision_id: str,
        decisions_dir: Path,
    ) -> Path | None:
        """Search Active/ and Archive/ for a decision note by ID.

        Args:
            decision_id: The decision ID to find.
            decisions_dir: The 08-Decisions directory path.

        Returns:
            Path to the matching note, or None if not found.
        """
        for subfolder in ("Active", "Archive"):
            search_dir = decisions_dir / subfolder
            if not search_dir.exists():
                continue
            for md_file in search_dir.glob("*.md"):
                post = frontmatter.load(str(md_file))
                if post.get("id") == decision_id:
                    return md_file
        return None
