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

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import frontmatter

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    raw_privacy_tier,
    within_ceiling,
)
from creek.models import (
    Decision,
    DecisionCandidate,
    DecisionStatus,
    Frequency,
    Phase,
    PraxisPotential,
    PrivacyTier,
    _generate_decision_id,
)
from creek.vault.reader import (
    FRONTMATTER_LOAD_ERRORS,
    iter_vault_fragments,
    load_post_or_raise,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment

logger = logging.getLogger(__name__)

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

# All three sets below are built from ``.value`` rather than from the enum
# members themselves (issue #991). ``StrEnum`` members compare equal to their
# values, so a set of members satisfies every membership test a set of strings
# would -- which is exactly why the ``frozenset[str]`` annotations went
# unchallenged, and why the discrepancy only surfaced once a member survived
# ``_VALID_PHASES``' guard and reached ``frontmatter.dumps``, where PyYAML has
# no representer for it and raises. Holding the values makes the annotations
# true and keeps a member from being smuggled through a set that claims to
# contain only plain strings.

# Frequency pairs that indicate active deliberation when combined
# with explicit praxis potential and exploring confidence.
_DECISION_FREQUENCY_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        (Frequency.F1.value, Frequency.F4.value),
        (Frequency.F1.value, Frequency.F5.value),
    },
)

# Statuses that map to the Active/ folder; all others go to Archive/.
_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {
        DecisionStatus.SENSING.value,
        DecisionStatus.DELIBERATING.value,
        DecisionStatus.COMMITTING.value,
    },
)

_VALID_PHASES: frozenset[str] = frozenset(
    {
        DecisionStatus.SENSING.value,
        DecisionStatus.DELIBERATING.value,
        DecisionStatus.COMMITTING.value,
        DecisionStatus.ENACTED.value,
        DecisionStatus.REFLECTING.value,
    },
)

_SOURCE_FRAGMENTS_HEADING: str = "## source fragments"
"""Lowercased body heading whose first bullet names a note's source fragment.

Compared against ``line.strip().lower()``, so a note that capitalises the
heading differently still identifies itself.
"""

_MAX_FILENAME_ORDINALS: int = 10_000
"""Cap on the paths :func:`_resolve_decision_note_path` probes.

Counts the unsuffixed name, so a cap of 3 means ``stem``, ``stem-1``,
``stem-2``. Mirrors ``creek.vault.writer._MAX_FILENAME_COLLISION_RETRIES``:
high enough that no real vault reaches it, low enough that a runaway
collision pattern surfaces as a loud ``RuntimeError`` rather than an
infinite loop. The probe is
dearer than the writer's — it parses each occupant rather than attempting an
exclusive create — so a stem shared by *n* identities costs O(n²) reads across
the batch. That is the same cost profile the writer already accepts, and it
only bites a vault that has already gone degenerate.
"""


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


def _decision_source_fragment_id(post: frontmatter.Post) -> str | None:
    """Return the source fragment id *post* records, or ``None``.

    The identity rule, in one place: the first bullet under the note body's
    ``## Source Fragments`` heading. ``None`` means the note declares no
    source fragment at all — a hand-written Decision note, or one whose
    generated section the operator removed — and therefore that its identity
    cannot be positively established.

    Args:
        post: A parsed Decision note.

    Returns:
        The recorded source fragment id, or ``None``.
    """
    in_source_section = False
    for line in post.content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_source_section = stripped.lower() == _SOURCE_FRAGMENTS_HEADING
            continue
        if in_source_section and stripped.startswith("- "):
            return stripped[2:].strip()
    return None


def _recorded_decision_source(note: Path) -> str | None:
    """Return the source fragment id a Decision note on disk records, or ``None``.

    Args:
        note: Path to a candidate markdown note.

    Returns:
        The recorded source fragment id, or ``None``.
    """
    post = _load_post(note)
    return None if post is None else _decision_source_fragment_id(post)


def _resolve_decision_note_path(
    target_dir: Path,
    stem: str,
    fragment_id: str,
) -> Path:
    """Return the path *fragment_id*'s Decision note owns under *target_dir*.

    Probes ``stem.md``, ``stem-1.md``, ``stem-2.md``, … and returns the first
    path that is either free or already occupied by a note recording
    *fragment_id*. A path occupied by anything else — a different fragment,
    or a note whose identity :func:`_recorded_decision_source` cannot
    establish — is skipped, never overwritten.

    Deliberately *not* ``VaultWriter._atomic_create``: that helper advances on
    occupancy, so it would mint a new file every run and never converge. This
    one advances on ownership, so the file count is bounded by the number of
    distinct source fragments.

    Only exact paths are probed — never ``glob``/``iterdir``, which would make
    a batch quadratic in directory size at 35k-fragment scale.

    Args:
        target_dir: Directory the note will be written into.
        stem: Filename stem (without ``.md``) the candidate naturally wants.
        fragment_id: The candidate's source fragment id — its identity.

    Returns:
        The path to write to.

    Raises:
        RuntimeError: If every one of :data:`_MAX_FILENAME_ORDINALS` probed
            paths belongs to another fragment.
    """
    # The ordinal loop is duplicated in creek/generate/compost.py on purpose.
    # Hoisting a shared filename builder is explicitly out of scope for #1334
    # and is issue #1417's job; merging them here would pre-empt it.
    for ordinal in range(_MAX_FILENAME_ORDINALS):
        suffix = "" if ordinal == 0 else f"-{ordinal}"
        note_path = target_dir / f"{stem}{suffix}.md"
        if (
            not note_path.exists()
            or _recorded_decision_source(note_path) == fragment_id
        ):
            return note_path
    msg = (
        f"Could not allocate a unique filename for '{stem}.md' in "
        f"{target_dir} after {_MAX_FILENAME_ORDINALS} attempts"
    )
    raise RuntimeError(msg)


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

        **This method applies no privacy screen and trusts its caller.** The
        ``intimate`` screen added by #1431 lives upstream in
        :func:`_admitted_decision_fragments`, which filters the *fragments*
        before they ever become candidates — a :class:`DecisionCandidate`
        carries no tier field, so by the time one reaches here the information
        needed to screen it is gone. Production is covered, because
        :func:`generate_decisions` is the only live producer into
        ``08-Decisions``. A caller that builds a candidate itself and invokes
        this method directly inherits the responsibility: ``candidate
        .fragment_title`` is written verbatim into both the ``title:`` front
        matter and the filename stem.

        The filename is ``<date>-<sanitised fragment title>[-N].md``, falling
        back to ``<date>[-N].md`` when the title sanitises to nothing. Because
        that stem is derived from the title alone, two different fragments can
        want it — :func:`_sanitize_title`'s 80-character truncation
        manufactures collisions by itself, and every untitled candidate of a
        given day wants the same bare date — so the path is resolved by
        *ownership* rather than by name: :func:`_resolve_decision_note_path`
        probes ``-1``, ``-2``, … until it finds a path that is free or already
        records this candidate's source fragment. **A note recording a
        different fragment is never overwritten** (#1334).

        **A note this candidate already owns is refreshed in place, and the
        refresh is destructive.** The rewritten note carries a freshly minted
        ``id:`` and its ``status:`` resets to ``sensing``, discarding whatever
        phase the operator had advanced it to along with any prose they added
        beneath the generated sections. That is today's behaviour and is
        preserved deliberately: :func:`generate_decisions` is index-gated and
        never calls this method for a fragment already captured, so production
        does not reach that branch. Callers that invoke this method directly
        inherit the clobber.

        "Already owns" means *at today's stem*. The stem embeds
        ``date.today()``, so a note this candidate owns from an earlier day
        is never probed, and a direct call on a later day writes a second
        note rather than refreshing the first. ``creek/generate/paradox.py``
        carries the same caveat for the same reason (#1320): a filename
        containing the date is not a stable identity across runs. Production
        is unaffected — the index spans both folders and every date, so
        :func:`generate_decisions` skips the candidate before the stem is
        ever built.

        Args:
            candidate: The DecisionCandidate to create a note for.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the created decision note file.

        Raises:
            RuntimeError: If :data:`_MAX_FILENAME_ORDINALS` consecutive
                candidate filenames all belong to other fragments.
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
        stem = f"{today.isoformat()}-{sanitized}" if sanitized else today.isoformat()
        note_path = _resolve_decision_note_path(
            target_dir,
            stem,
            candidate.fragment_id,
        )

        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    def update_decision_phase(
        self,
        decision_id: str,
        new_phase: DecisionStatus | str,
        vault_path: Path,
    ) -> Path:
        """Update a decision's phase and move between Active/Archive folders.

        Active statuses (sensing, deliberating, committing) live in
        ``08-Decisions/Active/``. Completed statuses (enacted, reflecting)
        live in ``08-Decisions/Archive/``.

        A ``DecisionStatus`` member is accepted as well as its value and is
        narrowed to that value on the way in (issue #991). Callers naturally
        reach for the member, and the validity guard has always accepted one
        -- ``StrEnum`` compares equal to its value -- but the member itself
        has no PyYAML representer, so writing it unconverted raised
        ``RepresenterError`` from ``frontmatter.dumps`` rather than producing
        a note. Converting once, here at the boundary, is what keeps the
        vault free of enum objects.

        Args:
            decision_id: The ID of the decision to update.
            new_phase: The new phase, as a ``DecisionStatus`` member or as
                the equivalent string.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            Path to the (possibly moved) decision note.

        Raises:
            ValueError: If the decision ID is not found or the phase is invalid.
        """
        phase = str(new_phase)
        if phase not in _VALID_PHASES:
            msg = f"Invalid decision phase: {new_phase!r}"
            raise ValueError(msg)

        decisions_dir = vault_path / "08-Decisions"
        source_path = self._find_decision_by_id(decision_id, decisions_dir)
        if source_path is None:
            msg = f"Decision {decision_id!r} not found in vault"
            raise ValueError(msg)

        # Update frontmatter
        post = load_post_or_raise(source_path)
        post["status"] = phase
        source_path.write_text(frontmatter.dumps(post), encoding="utf-8")

        # Determine correct folder
        target_subfolder = "Active" if phase in _ACTIVE_STATUSES else "Archive"
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

        An unreadable neighbour costs itself, not the search: ``08-Decisions``
        is an operator-editable folder, and this load was unguarded, so one
        hand-edited note there — a non-string frontmatter key raises
        ``TypeError`` out of ``frontmatter.load``'s ``**metadata`` splat —
        aborted every lookup in the folder rather than being stepped over
        (#924, #1475). Skipping is the same policy the module's other loader,
        :func:`_load_post`, already applies, and it is safe here because the
        skipped file could not have answered the query anyway: its ``id`` is
        exactly what could not be read. The cost is the one #926 tracks — the
        note is invisible, so a subsequent write may duplicate it.

        Returns:
            Path to the matching note, or None if not found.
        """
        for subfolder in ("Active", "Archive"):
            search_dir = decisions_dir / subfolder
            if not search_dir.exists():
                continue
            for md_file in search_dir.glob("*.md"):
                try:
                    post = frontmatter.load(str(md_file))
                except FRONTMATTER_LOAD_ERRORS:
                    logger.debug("Skipping unreadable decision note: %s", md_file)
                    continue
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

    ``yaml.YAMLError`` is caught alongside ``OSError`` and ``ValueError``
    because it is neither: a note whose frontmatter will not parse used to
    escape this helper and crash the caller. Every call site already treats
    ``None`` as "skip this note", so widening the tuple is what they all
    meant — and :func:`_recorded_decision_source` now depends on it, having
    taken over the inline handler
    :func:`_existing_decision_fragment_ids` used to carry. Same bug class as
    issue #1416.

    The tuple is **not** exhaustive, and this docstring should not be read
    as claiming it is: ``frontmatter.load`` passes the parsed mapping as
    keyword arguments, so a non-string YAML key (``2024-01-01:``, ``true:``,
    ``1:``) still raises ``TypeError`` past this handler. The synchronicity
    scans closed that gap by dropping ``frontmatter`` for the header-only
    :func:`creek.vault.links.read_header_meta` (#1416); this loader has not
    yet followed, and the remaining gap is tracked on #1475 rather than
    widened blindly here, because ``TypeError`` also covers genuine
    programming errors that should not be silently skipped.

    Args:
        path: The markdown file path.

    Returns:
        Loaded post, or ``None`` if the file cannot be read or parsed.
    """
    try:
        return frontmatter.load(str(path))
    except FRONTMATTER_LOAD_ERRORS:
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
        post = load_post_or_raise(decision_path)
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
    post = load_post_or_raise(note_path)
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

    Walks ``08-Decisions/{Active,Archive}/`` and reads each note's identity
    through :func:`_recorded_decision_source` — the same extractor the note
    writer resolves filenames with, so the index and the writer can never
    disagree about who owns what (#1334). A re-run therefore skips candidates
    whose Decision note already exists. Unreadable notes are skipped rather
    than crashing the report.

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
            fragment_id = _recorded_decision_source(md_file)
            if fragment_id is not None:
                seen.add(fragment_id)
    return seen


def _admitted_decision_fragments(
    vault_path: Path,
    override: PrivacyTierOverride,
) -> tuple[list[Fragment], int]:
    """Return the fragments decisions may name, and how many were withheld (#1431).

    Two independent conditions, in this order:

    1. :func:`~creek.classify.privacy_filter.within_ceiling` — the shared #968
       tier ceiling. This is the operator's dial: ``--include-tier``, or the
       MCP ``privacy_tier_ceiling``, or the ``ALL`` that ``creek fill`` states
       for every step because it has no tier flag of its own.
    2. An **unconditional** intimate screen. This one is not a dial, and it is
       not defeasible by any caller. It is here because ``decisions`` is the
       one report whose artifact carries a source fragment's title in its
       *filename*: ``create_decision_note`` sanitises ``fragment_title`` into
       the stem, and that name is then visible to Obsidian's file pane, to
       ``ls``, to Spotlight and to any sync client, with no front matter
       attached that could declare what tier it came from. A ceiling of ``ALL``
       is a reasonable instruction to include intimate *content* in a report;
       it is not an instruction to spell an intimate title out in the vault
       tree. (#1431 chose this — screen the fragment and write nothing —
       over stubbing the title, because a stubbed title collapses every
       intimate candidate onto the bare ``<date>`` stem that untitled
       candidates already contend for under
       :data:`_MAX_FILENAME_ORDINALS`.)

    The screen reads :func:`~creek.classify.privacy_filter.raw_privacy_tier`,
    not the model's ``fragment.privacy_tier``, for two reasons. It is the
    reader ``within_ceiling`` itself reduces to, so the gate and the screen can
    never end up holding two different tier opinions inside one expression. And
    it fails closed: a note whose front matter has **no** ``privacy_tier:`` key
    ranks ``intimate`` rather than the model's ``unclassified``, which is the
    answer ``tests/test_mcp_report_tier_ceiling.py``'s
    ``test_fragment_with_no_privacy_tier_key_fails_closed_to_intimate``
    already pins for report artifacts. The model reader would leave the #1431
    filename reproducible for exactly that fragment shape. An *explicit*
    ``privacy_tier: unclassified`` is unaffected — it ranks with ``personal``
    and still gets its note — and every pipeline-written fragment states the
    key, because :meth:`creek.vault.writer.VaultWriter._write_model`
    serialises ``model_dump(mode="json")``.

    This differs from the sibling rule in :mod:`creek.generate.compost`, and
    the difference is deliberate rather than an oversight:
    ``CompostTracker.__init__`` takes ``skip_intimate: bool = True``, a
    constructor *default* a caller can switch off, forced on only inside
    ``run_compost_scan`` — and forced there because that scan feeds fragment
    bodies to a potentially cloud-hosted verifier. Decisions makes no LLM call
    at all; its harm is local filesystem visibility, which no caller has a
    legitimate reason to opt into.

    Withheld fragments are counted rather than named. The count exists so the
    screen cannot disable a report in silence — see :func:`generate_decisions`
    — and it is a count and nothing else because the title is the leaking
    value.

    Args:
        vault_path: Root of the Obsidian vault.
        override: The #968 tier ceiling to apply.

    Returns:
        ``(admitted fragments, number withheld by the intimate screen)``. The
        count covers only condition 2; fragments refused by the ceiling are not
        counted, since that refusal is what the operator asked for.
    """
    admitted: list[Fragment] = []
    withheld = 0
    for _path, fragment, _body, raw in iter_vault_fragments(
        vault_path / "01-Fragments",
    ):
        # Kept as a literal ``within_ceiling(...)`` call resolved from module
        # globals, never folded into ``tier_within_override(raw_privacy_tier(
        # raw), override)``: the inverted-gate obedience proof in
        # tests/test_mcp_report_tier_ceiling.py monkeypatches
        # ``creek.generate.decisions.within_ceiling`` and asserts it was
        # actually called.
        if not within_ceiling(raw, override):
            continue
        if raw_privacy_tier(raw) is PrivacyTier.INTIMATE:
            withheld += 1
            continue
        admitted.append(fragment)
    return admitted, withheld


@dataclass(frozen=True, slots=True)
class DecisionsReport:
    """What one :func:`generate_decisions` run wrote, and what it refused.

    Deliberately a plain dataclass rather than a tuple or a
    :class:`typing.NamedTuple`, and that is a correctness choice rather than a
    stylistic one. ``creek_mcp/tools/report.py`` consumes this function's
    result as ``list(generate_decisions(...))``; against an *iterable* return
    that call site would have kept type-checking and kept running, while
    silently yielding ``[[Path, ...], 0]`` — a list whose first element is a
    list and whose second is an int — straight into the MCP ``report_paths``
    envelope. A non-iterable dataclass turns the same mistake into a
    mypy-strict error at check time and a ``TypeError`` at run time, so no
    call site can be missed when the return value grows a field (#1487).

    ``notes`` is a ``tuple``, not a ``list``, so the report is immutable
    through and through: a caller that mistakes it for the old ``list[Path]``
    and appends to it fails loudly instead of mutating a result nobody
    re-reads.

    Attributes:
        notes: Paths of the Decision notes this run newly wrote, in write
            order. Empty when there was nothing new to write.
        withheld: How many fragments the unconditional intimate screen in
            :func:`_admitted_decision_fragments` refused. Counts that screen
            only — never a ceiling refusal, which is what the operator asked
            for. :func:`withheld_notice` turns it into the wording both the
            log and the console use.
    """

    notes: tuple[Path, ...]
    withheld: int


def withheld_notice(withheld: int) -> str | None:
    """Return the operator-facing explanation of a withholding, or ``None``.

    This is the **single** source of that wording. It feeds both the
    ``logger.warning`` in :func:`generate_decisions` and the console message
    ``creek.cli._report_decisions`` prints, so a maintainer reading a log and
    an operator reading a terminal cannot be handed two different
    explanations of the same refusal (#1487).

    The count is stated as "fragment(s)", never "candidate(s)": it is taken
    *before* decision detection runs, so it includes withheld fragments that
    were never decision candidates at all.

    Both remedies the notice names were checked against what the pipeline
    actually does. ``creek classify`` assigns a tier outright to a keyless
    note (``creek/classify/privacy_pass.py:165-169`` — the ratchet is reached
    only for a tier already on disk), so re-running it genuinely readmits
    one; but ``creek/classify/privacy.py:136-140`` tiers a self-authored
    ``platform: journal`` fragment ``INTIMATE`` unconditionally, and that is
    the commonest keyless shape, so the exception is stated rather than left
    for the operator to discover. Editing ``privacy_tier:`` by hand is the
    remedy for that case and for an already-recorded ``intimate``.

    The wording carries no square bracket anywhere on purpose: the console it
    reaches is a Rich console, which would eat ``[anything]`` as a style tag
    and drop the clause from the terminal while leaving it in the log.

    Args:
        withheld: Number of fragments the intimate screen refused.

    Returns:
        The notice, or ``None`` when nothing was withheld. ``None`` rather
        than an empty string is what keeps the notice conditional by
        construction at both call sites.
    """
    if withheld <= 0:
        return None
    return (
        f"{withheld} fragment(s) withheld from the decisions report: intimate "
        "tier, or no privacy_tier key at all (which fails closed to intimate). "
        "Re-run `creek classify` to tier a keyless note — a self-authored "
        "journal note is tiered intimate and stays out — or set privacy_tier "
        "by hand. A tier already recorded as intimate is never lowered."
    )


def generate_decisions(
    vault_path: Path,
    *,
    override: PrivacyTierOverride = PrivacyTierOverride.ALL,
) -> DecisionsReport:
    """Detect decision candidates across the vault and write new notes (#581).

    Loads fragments via :func:`creek.vault.reader.iter_vault_fragments` (the
    shared reader the other vault-iterating reports use), runs
    :meth:`DecisionDetector.detect_decisions`, and writes one Decision note per
    candidate whose source fragment is not already captured by an existing note
    — so re-running is idempotent and never duplicates notes for one fragment.

    Since #1334 that guarantee holds in both directions: the index gate stops
    one fragment getting two notes, and :func:`_resolve_decision_note_path`
    stops two distinct candidates sharing one *file*. Before the fix, two
    candidates whose titles sanitised to the same stem landed on the same
    path; the second write destroyed the first, the index only ever saw the
    survivor, and every later run re-detected and re-wrote the loser —
    oscillating forever and churning the surviving note's ``id:`` each time.

    A vault already damaged that way heals on the next run: the survivor is
    left byte-for-byte alone and the fragment it displaced is written to the
    next free ordinal. Content the pre-fix clobber already destroyed is gone
    and cannot be recovered.

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

            The ceiling is **not** the whole admission rule here, and since
            #1431 it is not the strictest part of it. The stakes are unusually
            concrete: a generated note's ``title:`` frontmatter is a source
            fragment's title verbatim, and its filename is that same title
            sanitised — plus a ``-N`` ordinal when another fragment already
            holds the name. So on top of the ceiling,
            :func:`_admitted_decision_fragments` applies an unconditional
            ``intimate`` screen that **no value of this argument can lift**,
            reading the fail-closed
            :func:`~creek.classify.privacy_filter.raw_privacy_tier`. See that
            helper for why the reader is the fail-closed one and why this rule
            is non-defeasible where compost's ``skip_intimate`` is a default.

            How a screened fragment gets back in is worth stating precisely,
            because getting it wrong prints a false remedy at the operator.
            The ``privacy_tier`` ratchet binds a tier **already recorded on
            disk** (``creek/classify/privacy_pass.py:165-169``: ``escalate``
            is reached only on the ``not assigning`` branch), so an explicit
            ``intimate`` is never lowered by ``creek classify``, even with
            ``--force``, and a hand edit of the front matter is the only way
            back in for that fragment. A note with **no** ``privacy_tier:``
            key is not ratcheted at all — ``needs_tier`` is ``True`` when the
            key is absent, so ``creek classify`` assigns it a tier outright
            and can readmit it. The caveat that decides the common case:
            ``creek/classify/privacy.py:136-140`` classifies a self-authored
            ``platform: journal`` fragment ``INTIMATE`` unconditionally, so
            for the commonest keyless shape re-running ``creek classify``
            does *not* readmit it, and a hand edit of ``privacy_tier:`` is
            again the remedy.

    Returns:
        A :class:`DecisionsReport` carrying the newly written note paths and
        the number of fragments the intimate screen withheld. ``notes`` is
        empty when there are no new decision candidates — or when every
        candidate was screened, and telling those two cases apart is exactly
        why ``withheld`` travels back to the caller instead of dying in the
        log (#1487).
    """
    fragments, withheld = _admitted_decision_fragments(vault_path, override)
    notice = withheld_notice(withheld)
    if notice is not None:
        logger.warning("%s", notice)
    detector = DecisionDetector()
    already = _existing_decision_fragment_ids(vault_path)
    written: list[Path] = []
    for candidate in detector.detect_decisions(fragments):
        if candidate.fragment_id in already:
            continue
        written.append(detector.create_decision_note(candidate, vault_path))
        already.add(candidate.fragment_id)
    return DecisionsReport(notes=tuple(written), withheld=withheld)
