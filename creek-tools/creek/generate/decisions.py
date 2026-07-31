"""Decision detection and context gathering for Creek decisions.

Detects decision-relevant content in Creek fragments using keyword matching
and frequency/confidence pattern analysis, generates draft Decision notes
in the vault, manages decision phase transitions between Active and
Archive folders, and gathers cross-vault context (related threads, past
decisions, current wavelength, praxis, interventions) for active Decision
notes while enforcing the anti-manipulation guardrails from Section 12.4
of the Creek Ontology.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.classify.privacy_filter import PrivacyTierOverride, within_ceiling
from creek.models import (
    Decision,
    DecisionCandidate,
    DecisionStatus,
    Frequency,
    Phase,
    PraxisPotential,
    _generate_decision_id,
)
from creek.vault.reader import iter_vault_fragments

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


def _build_frequency_context(fragment: Fragment) -> list[Frequency]:
    """Extract all frequency values from a fragment.

    Args:
        fragment: The source fragment.

    Returns:
        List of ``Frequency`` enum values (e.g. ``[Frequency.F1, Frequency.F5]``),
        excluding the ``UNCLASSIFIED`` primary.
    """
    freqs: list[Frequency] = []
    primary = Frequency(fragment.frequency.primary)
    if primary != Frequency.UNCLASSIFIED:
        freqs.append(primary)
    for sec in fragment.frequency.secondary:
        freqs.append(Frequency(sec))
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
            status=str(DecisionStatus.SENSING),  # noqa: FURB123  # yaml.safe_dump cannot serialise StrEnum directly; convert to plain str.
            opened=today.isoformat(),
            wavelength_phase_at_opening=candidate.wavelength_phase_at_detection,
            frequency_context=candidate.frequency_context.copy(),
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
        all_freqs.update(str(sec) for sec in fragment.frequency.secondary)

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


# ---- Decision Context Gathering (Section 12.3 - 12.5) ----


_FREQUENCY_COLOR: dict[str, str] = {
    Frequency.F1: "beige",
    Frequency.F2: "purple",
    Frequency.F3: "red",
    Frequency.F4: "blue",
    Frequency.F5: "orange",
    Frequency.F6: "green",
    Frequency.F7: "yellow",
    Frequency.F8: "teal",
    Frequency.F9: "ultraviolet",
    Frequency.F10: "clear_light",
}
"""Canonical mapping from APTITUDE frequency codes to ontology color names."""


PRACTICE_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("beige", "rising"): ("Cold Shower",),
    ("purple", "bottoming_out"): ("Listening to Music",),
    ("red", "bottoming_out"): ("Pranayama (Lion's Breath)",),
    ("blue", "rising"): ("Metta Meditation",),
    ("orange", "rising"): ("Pranayama (Wim Hof)",),
    ("green", "rising"): ("Yoga", "Creative Writing"),
    ("yellow", "peaking"): ("Samatha Vipassana",),
    ("teal", "peaking"): ("Magick",),
    ("ultraviolet", "peaking"): ("Meditation Retreats",),
    ("green", "diminishing"): ("Journaling",),
    ("teal", "bottoming_out"): ("Baby Waterfall Conventions",),
}
"""Phase x Frequency intervention map (Ontology Section 12.5).

Keys are ``(frequency_color, phase_value)`` pairs; values are tuples of
practice names mapped by the Archetypal Wavelength framework as useful in
that territory. Loaded at module import time; see
:func:`DecisionContextGatherer.interventions_lookup`.
"""


_DIRECTIVE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\byou should\b", re.IGNORECASE), "one option is to"),
    (re.compile(r"\byou need to\b", re.IGNORECASE), "one possibility is to"),
    (re.compile(r"\byou must\b", re.IGNORECASE), "one possibility is to"),
    (re.compile(r"\bthe best option\b", re.IGNORECASE), "one option"),
    (re.compile(r"\bthe right choice\b", re.IGNORECASE), "one possibility"),
    (re.compile(r"\bit'?s clear that\b", re.IGNORECASE), "one reading is that"),
    (re.compile(r"\bobviously\b", re.IGNORECASE), "from one perspective"),
    (re.compile(r"\bact now\b", re.IGNORECASE), "act when it feels right"),
    (
        re.compile(r"\bbefore it'?s too late\b", re.IGNORECASE),
        "when the moment feels right",
    ),
    (re.compile(r"\blast chance\b", re.IGNORECASE), "a moment that is present"),
    (
        re.compile(r"\bthis is permanent\b", re.IGNORECASE),
        "some considerations include",
    ),
    (
        re.compile(r"\bthis is irreversible\b", re.IGNORECASE),
        "some considerations include",
    ),
)
"""Directive/urgency/scarcity patterns replaced by descriptive language.

Section 12.4 of the Ontology prohibits the decision layer from recommending
options, weighting rationality over felt sense, or using urgency/scarcity
framing. The patterns above are scrubbed by
:meth:`DecisionContextGatherer.apply_guardrails`.
"""


_MIN_HISTORY_FOR_ADVISORY = 2
"""Minimum past-decision count required to surface a wavelength advisory."""


@dataclass
class DecisionContext:
    """Aggregated cross-vault context for a single Decision note.

    Attributes:
        decision_id: The ID of the decision this context is for.
        decision_title: The title of the decision this context is for.
        related_threads: IDs of threads semantically related to the decision.
        related_decisions: IDs of past decisions with similar themes.
        current_wavelength: Current wavelength phase of the human.
        relevant_praxis: IDs of praxis notes applicable to this decision.
        frequency_affinity: Frequency codes most active in this decision.
        interventions: Suggested practices from the Phase x Frequency map.
    """

    decision_id: str
    decision_title: str
    related_threads: list[str] = field(default_factory=list)
    related_decisions: list[str] = field(default_factory=list)
    current_wavelength: str = ""
    relevant_praxis: list[str] = field(default_factory=list)
    frequency_affinity: list[str] = field(default_factory=list)
    interventions: list[str] = field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    """Tokenise text into a lowercase word set, dropping short stopwords.

    Args:
        text: Raw text to tokenise.

    Returns:
        Set of tokens with length >= 3, lowercased.
    """
    return {tok for tok in re.findall(r"[a-zA-Z]+", text.lower()) if len(tok) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two sets.

    Args:
        a: First set.
        b: Second set.

    Returns:
        Ratio of intersection size to union size, or ``0.0`` if both
        sets are empty.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _load_post(path: Path) -> frontmatter.Post | None:
    """Safely load a frontmatter post from disk.

    Args:
        path: The markdown file path.

    Returns:
        Loaded post, or ``None`` if the file cannot be parsed.
    """
    try:
        return frontmatter.load(str(path))
    except (OSError, ValueError):
        return None


def _listify(value: object) -> list[str]:
    """Coerce a frontmatter value into a list of strings.

    Args:
        value: Raw value from a frontmatter field.

    Returns:
        A list of strings, empty if the input is falsy or unsupported.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


class DecisionContextGatherer:
    """Gather cross-vault context for Decision notes.

    Walks the vault to find related threads, past decisions, relevant
    praxis, and the current wavelength phase, then renders a Markdown
    ``## Context`` section. All generated text is passed through
    :meth:`apply_guardrails` to enforce the Section 12.4 anti-manipulation
    rules (no recommendations, no urgency/scarcity framing, no prescriptive
    language).

    Attributes:
        similarity_threshold: Minimum Jaccard similarity (0-1) for a
            thread or decision to count as related.
    """

    def __init__(self, similarity_threshold: float = 0.1) -> None:
        """Initialise the gatherer.

        Args:
            similarity_threshold: Minimum Jaccard similarity for
                related-thread and related-decision matching.
        """
        self.similarity_threshold = similarity_threshold

    # ---- Public API ----

    def gather_context(
        self,
        decision: Decision,
        vault_path: Path,
    ) -> DecisionContext:
        """Gather cross-vault context for a Decision.

        Args:
            decision: The Decision to gather context for.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            A DecisionContext with related threads, decisions, praxis,
            current wavelength, frequency affinity, and interventions.
        """
        tokens = _tokenize(decision.title)
        freqs = [str(f) for f in decision.frequency_context]

        related_threads = self._find_related_threads(vault_path, tokens, freqs)
        related_decisions = self._find_related_decisions(
            vault_path,
            decision.id,
            tokens,
            freqs,
        )
        relevant_praxis = self._find_relevant_praxis(vault_path, freqs)
        current_wavelength = self._current_wavelength(
            vault_path,
            fallback=decision.wavelength_phase_at_opening,
        )
        affinity = self._frequency_affinity(decision, relevant_praxis, vault_path)
        interventions = self._gather_interventions(affinity, current_wavelength)

        return DecisionContext(
            decision_id=decision.id,
            decision_title=decision.title,
            related_threads=related_threads,
            related_decisions=related_decisions,
            current_wavelength=current_wavelength,
            relevant_praxis=relevant_praxis,
            frequency_affinity=affinity,
            interventions=interventions,
        )

    def generate_context_section(self, context: DecisionContext) -> str:
        """Render a Markdown ``## Context`` section for the decision.

        The generated text is passed through :meth:`apply_guardrails`
        before return to enforce the Section 12.4 anti-manipulation rules.

        Args:
            context: Context to render.

        Returns:
            A Markdown section body (no trailing newline).
        """
        lines: list[str] = ["## Context", ""]
        lines.extend(self._render_threads(context.related_threads))
        lines.extend(self._render_decisions(context.related_decisions))
        lines.extend(self._render_praxis(context.relevant_praxis))
        lines.extend(self._render_frequency_affinity(context.frequency_affinity))
        lines.extend(
            self._render_wavelength(
                context.current_wavelength,
                len(context.related_decisions),
            ),
        )
        lines.extend(self._render_interventions(context.interventions))
        return self.apply_guardrails("\n".join(lines).rstrip() + "\n")

    def apply_guardrails(self, context_text: str) -> str:
        """Scrub directive, urgency, and permanence framing from text.

        Replaces patterns like "you should", "the best option", "act now",
        and "this is permanent" with descriptive, option-surfacing language.

        Args:
            context_text: Input Markdown text.

        Returns:
            Text with directive language replaced by descriptive phrasing.
        """
        result = context_text
        for pattern, replacement in _DIRECTIVE_REPLACEMENTS:
            result = pattern.sub(replacement, result)
        return result

    def interventions_lookup(
        self,
        frequency: str,
        phase: str,
    ) -> list[str]:
        """Look up practices mapped to a (frequency, phase) pair.

        The frequency argument may be either a Frequency code (``"F6"``) or
        a color name (``"green"``). Phase accepts any
        :class:`~creek.models.Phase` value.

        Args:
            frequency: APTITUDE frequency code or color name.
            phase: Archetypal wavelength phase.

        Returns:
            List of practice names from the Phase x Frequency map
            (Section 12.5). Empty list if no practices are mapped.
        """
        color = _FREQUENCY_COLOR.get(frequency, frequency).lower()
        phase_key = phase.lower()
        return list(PRACTICE_MAP.get((color, phase_key), ()))

    def append_context_section(
        self,
        decision_path: Path,
        context_section: str,
    ) -> Path:
        """Append a generated context section to a Decision note.

        If the note already contains a ``## Context`` section, the
        existing section is replaced; otherwise the new section is
        appended after the body.

        Args:
            decision_path: Path to the decision note.
            context_section: The generated Markdown context section.

        Returns:
            The path that was updated.
        """
        post = frontmatter.load(str(decision_path))
        body = post.content
        existing = re.search(r"\n## Context\b.*", body, flags=re.DOTALL)
        new_section = context_section.rstrip() + "\n"
        if existing:
            post.content = body[: existing.start()].rstrip() + "\n\n" + new_section
        else:
            post.content = body.rstrip() + "\n\n" + new_section
        decision_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return decision_path

    # ---- Related-thread / decision / praxis lookup ----

    def _find_related_threads(
        self,
        vault_path: Path,
        title_tokens: set[str],
        freqs: list[str],
    ) -> list[str]:
        """Find IDs of threads related by token overlap or frequency."""
        results: list[tuple[float, str]] = []
        for md_file in self._iter_markdown(vault_path / "02-Threads"):
            scored = self._score_thread(md_file, title_tokens, freqs)
            if scored is not None:
                results.append(scored)
        return [tid for _, tid in sorted(results, key=lambda r: -r[0])]

    def _score_thread(
        self,
        md_file: Path,
        title_tokens: set[str],
        freqs: list[str],
    ) -> tuple[float, str] | None:
        """Score a single thread file and return ``(score, id)`` if related."""
        post = _load_post(md_file)
        if post is None:
            return None
        thread_id = str(post.get("id") or "")
        if not thread_id:
            return None
        thread_tokens = _tokenize(
            str(post.get("title") or "") + " " + str(post.get("description") or ""),
        )
        score = _jaccard(title_tokens, thread_tokens)
        thread_freqs = _listify(post.get("frequency_affinity"))
        if any(f in thread_freqs for f in freqs):
            score = max(score, self.similarity_threshold)
        if score < self.similarity_threshold:
            return None
        return (score, thread_id)

    def _find_related_decisions(
        self,
        vault_path: Path,
        exclude_id: str,
        title_tokens: set[str],
        freqs: list[str],
    ) -> list[str]:
        """Find IDs of past decisions with similar themes or stakes."""
        decisions_dir = vault_path / "08-Decisions"
        results: list[tuple[float, str]] = []
        for subfolder in ("Active", "Archive"):
            for md_file in self._iter_markdown(decisions_dir / subfolder):
                scored = self._score_decision(
                    md_file,
                    exclude_id,
                    title_tokens,
                    freqs,
                )
                if scored is not None:
                    results.append(scored)
        return [did for _, did in sorted(results, key=lambda r: -r[0])]

    def _score_decision(
        self,
        md_file: Path,
        exclude_id: str,
        title_tokens: set[str],
        freqs: list[str],
    ) -> tuple[float, str] | None:
        """Score a single decision file and return ``(score, id)`` if related."""
        post = _load_post(md_file)
        if post is None:
            return None
        did = str(post.get("id") or "")
        if not did or did == exclude_id:
            return None
        their_tokens = _tokenize(str(post.get("title") or ""))
        score = _jaccard(title_tokens, their_tokens)
        their_freqs = _listify(post.get("frequency_context"))
        if any(f in their_freqs for f in freqs):
            score = max(score, self.similarity_threshold)
        if score < self.similarity_threshold:
            return None
        return (score, did)

    def _find_relevant_praxis(
        self,
        vault_path: Path,
        freqs: list[str],
    ) -> list[str]:
        """Find IDs of praxis notes whose frequencies overlap the decision."""
        praxis_dir = vault_path / "04-Praxis"
        results: list[str] = []
        for md_file in self._iter_markdown(praxis_dir):
            post = _load_post(md_file)
            if post is None:
                continue
            pid = str(post.get("id") or "")
            if not pid:
                continue
            praxis_freqs = _listify(post.get("frequency"))
            if not freqs or any(f in praxis_freqs for f in freqs):
                results.append(pid)
        return results

    # ---- Wavelength + frequency affinity ----

    def _current_wavelength(self, vault_path: Path, fallback: str) -> str:
        """Determine current wavelength phase.

        Reads the most recent WavelengthObservation note from
        ``05-Wavelength/Observations/`` (by ``date`` field). Falls back
        to the value carried on the decision if no observation is found.

        Args:
            vault_path: Vault root.
            fallback: Phase string to use when no observation is available.

        Returns:
            A phase value (e.g. ``"rising"``), or the fallback.
        """
        obs_dir = vault_path / "05-Wavelength" / "Observations"
        most_recent: tuple[str, str] | None = None
        for md_file in self._iter_markdown(obs_dir):
            post = _load_post(md_file)
            if post is None:
                continue
            date_str = str(post.get("date") or "")
            phase_val = str(post.get("phase") or "")
            if not date_str or not phase_val:
                continue
            if most_recent is None or date_str > most_recent[0]:
                most_recent = (date_str, phase_val)
        return most_recent[1] if most_recent else fallback

    def _frequency_affinity(
        self,
        decision: Decision,
        relevant_praxis_ids: list[str],
        vault_path: Path,
    ) -> list[str]:
        """Rank frequencies by prevalence across decision and praxis.

        Args:
            decision: The source decision.
            relevant_praxis_ids: IDs of related praxis notes.
            vault_path: Vault root (used to inspect praxis frequencies).

        Returns:
            Ordered list of frequency codes, most-active first.
        """
        counts: dict[str, int] = {}
        for decision_freq in decision.frequency_context:
            key = str(decision_freq)
            counts[key] = counts.get(key, 0) + 2
        for md_file in self._iter_markdown(vault_path / "04-Praxis"):
            post = _load_post(md_file)
            if post is None:
                continue
            if str(post.get("id") or "") not in relevant_praxis_ids:
                continue
            for praxis_freq in _listify(post.get("frequency")):
                counts[praxis_freq] = counts.get(praxis_freq, 0) + 1
        return [f for f, _ in sorted(counts.items(), key=lambda item: -item[1])]

    def _gather_interventions(
        self,
        affinity: list[str],
        phase: str,
    ) -> list[str]:
        """Collect unique interventions for the active frequencies+phase."""
        if not phase:
            return []
        seen: list[str] = []
        for freq in affinity:
            for practice in self.interventions_lookup(freq, phase):
                if practice not in seen:
                    seen.append(practice)
        return seen

    # ---- Markdown rendering helpers ----

    @staticmethod
    def _render_threads(threads: list[str]) -> list[str]:
        """Render the ``### Related Threads`` section."""
        if not threads:
            return ["### Related Threads", "", "_No related threads surfaced._", ""]
        lines = ["### Related Threads", ""]
        lines.extend(f"- [[{tid}]]" for tid in threads)
        lines.append("")
        return lines

    @staticmethod
    def _render_decisions(decisions: list[str]) -> list[str]:
        """Render the ``### Related Past Decisions`` section."""
        if not decisions:
            return [
                "### Related Past Decisions",
                "",
                "_No comparable past decisions surfaced._",
                "",
            ]
        lines = ["### Related Past Decisions", ""]
        lines.extend(f"- [[{did}]]" for did in decisions)
        lines.append("")
        return lines

    @staticmethod
    def _render_praxis(praxis: list[str]) -> list[str]:
        """Render the ``### Relevant Praxis`` section."""
        if not praxis:
            return ["### Relevant Praxis", "", "_No praxis notes surfaced._", ""]
        lines = ["### Relevant Praxis", ""]
        lines.extend(f"- [[{pid}]]" for pid in praxis)
        lines.append("")
        return lines

    @staticmethod
    def _render_frequency_affinity(affinity: list[str]) -> list[str]:
        """Render the ``### Frequency Affinity`` section."""
        lines = ["### Frequency Affinity", ""]
        if affinity:
            lines.append(
                "Most active frequencies in this decision: "
                + ", ".join(affinity)
                + ".",
            )
        else:
            lines.append("_Frequency affinity is not yet established._")
        lines.append("")
        return lines

    @staticmethod
    def _render_wavelength(phase: str, past_decision_count: int) -> list[str]:
        """Render the ``### Current Wavelength`` section and advisory."""
        lines = ["### Current Wavelength", ""]
        if not phase:
            lines.extend(("_Current wavelength phase is unknown._", ""))
            return lines
        lines.append(f"You are currently in the **{phase}** phase.")
        if past_decision_count >= _MIN_HISTORY_FOR_ADVISORY:
            lines.extend(
                (
                    "",
                    f"Historically, decisions made during {phase} have had "
                    "a pattern worth noticing; review the related past "
                    "decisions above.",
                )
            )
        lines.append("")
        return lines

    @staticmethod
    def _render_interventions(interventions: list[str]) -> list[str]:
        """Render the ``### Interventions Surfaced`` section."""
        if not interventions:
            return []
        lines = ["### Interventions Surfaced", ""]
        lines.extend(
            (
                "Practices that have been mapped as useful in this territory:",
                "",
            )
        )
        lines.extend(f"- {practice}" for practice in interventions)
        lines.append("")
        return lines

    @staticmethod
    def _iter_markdown(directory: Path) -> list[Path]:
        """List markdown files in a directory, safely handling absence."""
        if not directory.exists():
            return []
        return sorted(directory.glob("*.md"))


def _coerce_phase(raw: object) -> str:
    """Coerce a raw phase value to a known Phase string or empty."""
    value = str(raw or "")
    valid = {p.value for p in Phase}
    return value if value == "" or value in valid else ""


def _coerce_opened(raw: object) -> date | None:
    """Coerce a raw ``opened`` value to a date, or ``None`` if unparseable."""
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def decision_from_note(note_path: Path) -> Decision:
    """Load a Decision Pydantic model from a note file.

    Args:
        note_path: Path to a Decision markdown note.

    Returns:
        A :class:`~creek.models.Decision` built from the note's
        frontmatter. Missing fields fall back to the model defaults.
    """
    post = frontmatter.load(str(note_path))
    metadata = post.metadata.copy()
    metadata["title"] = metadata.get("title") or note_path.stem
    metadata["id"] = metadata.get("id") or _generate_decision_id()
    valid_freqs = {freq.value for freq in Frequency}
    metadata["frequency_context"] = [
        f for f in _listify(metadata.get("frequency_context")) if f in valid_freqs
    ]
    metadata["wavelength_phase_at_opening"] = _coerce_phase(
        metadata.get("wavelength_phase_at_opening"),
    )
    opened = _coerce_opened(metadata.get("opened"))
    if opened is not None:
        metadata["opened"] = opened
    else:
        metadata.pop("opened", None)
    return Decision.model_validate(metadata)


def _existing_decision_fragment_ids(vault_path: Path) -> set[str]:
    """Return the source fragment ids already captured by Decision notes (#581).

    Scans ``08-Decisions/{Active,Archive}/`` and reads each note's first
    ``## Source Fragments`` bullet — the source fragment id — so a re-run can
    skip candidates whose decision note already exists. Unreadable notes are
    skipped rather than crashing the report.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        The set of source fragment ids already recorded in Decision notes.
    """
    seen: set[str] = set()
    decisions_dir = vault_path / "08-Decisions"
    for subfolder in ("Active", "Archive"):
        folder = decisions_dir / subfolder
        if not folder.is_dir():
            continue
        for md_file in folder.rglob("*.md"):
            try:
                post = frontmatter.load(str(md_file))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            in_source_section = False
            for line in post.content.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    in_source_section = stripped.lower() == "## source fragments"
                    continue
                if in_source_section and stripped.startswith("- "):
                    seen.add(stripped[2:].strip())
                    break
    return seen


def generate_decisions(
    vault_path: Path,
    *,
    override: PrivacyTierOverride = PrivacyTierOverride.ALL,
) -> list[Path]:
    """Detect decision candidates across the vault and write new notes (#581).

    Loads fragments via :func:`creek.vault.reader.iter_vault_fragments` (the
    shared reader the other vault-iterating reports use), runs
    :meth:`DecisionDetector.detect_decisions`, and writes one Decision note per
    candidate whose source fragment is not already captured by an existing note
    — so re-running is idempotent and never duplicates notes for one fragment.

    Args:
        vault_path: Root of the Obsidian vault.
        override: Tier ceiling (#968), applied to each fragment's *raw*
            frontmatter — which the shared reader already yields, so no
            second read is introduced. Defaults to
            :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
            meaning "no ceiling declared" — a genuine no-op for callers that
            predate #968, and safe because both production report surfaces
            state an override explicitly (pinned by
            ``tests/test_mcp_report_tier_ceiling.py``'s
            ``test_production_report_callers_always_state_an_override``).
            The stakes here are unusually concrete: a generated note's
            ``title:`` frontmatter *and* its filename are a source
            fragment's title verbatim.

    Returns:
        Paths of the newly written Decision notes; empty when there are no new
        decision candidates.
    """
    fragments = [
        fragment
        for _path, fragment, _body, raw in iter_vault_fragments(
            vault_path / "01-Fragments",
        )
        if within_ceiling(raw, override)
    ]
    detector = DecisionDetector()
    already = _existing_decision_fragment_ids(vault_path)
    written: list[Path] = []
    for candidate in detector.detect_decisions(fragments):
        if candidate.fragment_id in already:
            continue
        written.append(detector.create_decision_note(candidate, vault_path))
        already.add(candidate.fragment_id)
    return written
