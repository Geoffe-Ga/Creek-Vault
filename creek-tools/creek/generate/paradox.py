"""Paradox Preservation — detect contradictions without resolving them.

Implements Section 10.2 of the Creek Ontology: when the system finds
contradictory stances across fragments from the same person, it does
*not* resolve the contradiction. Instead, a note is written to
``10-Liminal/Paradoxes/`` that links both fragments, describes the
detected tension in neutral language, and offers a reflection prompt.

Detection operates on four independent rules:

1. **Phase contradiction** — fragments with high semantic similarity that
   sit on opposite phases of the Archetypal Wavelength cycle.
2. **Confidence contradiction** — fragments on a shared thread carrying
   opposite confidence levels (e.g. ``musing`` vs ``settled``).
3. **Dosage contradiction** — fragments with the same primary frequency
   where one marks it ``medicine`` and the other ``toxic``.
4. **Keyword contradiction** — a fragment whose title contains an
   explicit contradiction phrase (``but actually``, ``I used to think``,
   ``contrary to what I said``) paired with a topically-linked fragment.

Each paradox links exactly two fragments. The generated note is
deliberately neutral: no resolution, prescription, or judgment.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import frontmatter

from creek.models import Confidence, Dosage, Phase

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment


CONTRADICTION_KEYWORDS: tuple[str, ...] = (
    "but actually",
    "i used to think",
    "contrary to what i said",
)
"""Case-insensitive phrases that mark a fragment as self-aware of contradiction."""


REFLECTION_PROMPT: str = (
    "These fragments hold tension with each other. "
    "What truth lives in the space between them?"
)
"""The reflection prompt appended to every paradox note.

Invites integration by sitting with the tension rather than resolving it.
"""


_DEFAULT_SIMILARITY_THRESHOLD: float = 0.7
"""Minimum cosine similarity for two fragments to count as the same topic."""


_EXCERPT_MAX_CHARS: int = 280
"""Maximum excerpt length written into a paradox note."""


_OPPOSITE_PHASE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({Phase.RISING.value, Phase.DIMINISHING.value}),
        frozenset({Phase.RISING.value, Phase.BOTTOMING_OUT.value}),
        frozenset({Phase.PEAKING.value, Phase.BOTTOMING_OUT.value}),
        frozenset({Phase.PEAKING.value, Phase.DIMINISHING.value}),
        frozenset({Phase.WITHDRAWAL.value, Phase.RESTORATION.value}),
    },
)
"""Archetypal Wavelength phase pairs treated as opposites for Rule 1."""


_OPPOSITE_CONFIDENCE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({Confidence.MUSING.value, Confidence.SETTLED.value}),
        frozenset({Confidence.MUSING.value, Confidence.CONVICTION.value}),
        frozenset({Confidence.EXPLORING.value, Confidence.SETTLED.value}),
        frozenset({Confidence.EXPLORING.value, Confidence.CONVICTION.value}),
    },
)
"""Confidence pairs treated as opposites for Rule 2."""


_OPPOSITE_DOSAGE_PAIR: frozenset[str] = frozenset(
    {Dosage.MEDICINE.value, Dosage.TOXIC.value},
)
"""Dosage pair treated as opposite for Rule 3."""


_CONTRADICTION_DESCRIPTIONS: dict[str, str] = {
    "phase": (
        "These fragments sit on opposite phases of the Archetypal "
        "Wavelength while sharing topic."
    ),
    "confidence": (
        "These fragments travel the same thread at very different "
        "levels of settled-ness."
    ),
    "dosage": (
        "The same frequency is experienced as medicine in one fragment "
        "and as toxic in the other."
    ),
    "keyword": (
        "One fragment explicitly marks a shift from a stance held in the other."
    ),
}
"""Neutral, descriptive summaries of each contradiction type."""


_FILENAME_SANITIZER = re.compile(r"[^\w\-]+")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors.

    Args:
        a: First embedding vector.
        b: Second embedding vector.

    Returns:
        Cosine similarity in ``[-1.0, 1.0]``; ``0.0`` if either vector
        is empty or has zero norm.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if 0.0 in (norm_a, norm_b):
        return 0.0
    return dot / (norm_a * norm_b)


def _fragment_frequencies(fragment: Fragment) -> set[str]:
    """Collect all classified frequency codes from a fragment.

    Args:
        fragment: The fragment whose frequency set to extract.

    Returns:
        Set of frequency code strings (``"F1"``, ``"F2"``, ...), excluding
        ``unclassified``.
    """
    freqs: set[str] = set()
    primary = str(fragment.frequency.primary)
    if primary and primary != "unclassified":
        freqs.add(primary)
    for sec in fragment.frequency.secondary:
        sec_str = str(sec)
        if sec_str and sec_str != "unclassified":
            freqs.add(sec_str)
    return freqs


def _excerpt(fragment: Fragment) -> str:
    """Return a short, clean excerpt for a fragment.

    Fragments carry only a title in the current model; the title is used
    as the excerpt, truncated at :data:`_EXCERPT_MAX_CHARS`.

    Args:
        fragment: The fragment to excerpt.

    Returns:
        A single-line excerpt string.
    """
    raw = fragment.title.strip()
    if len(raw) <= _EXCERPT_MAX_CHARS:
        return raw
    return raw[: _EXCERPT_MAX_CHARS - 1].rstrip() + "\u2026"


def _has_contradiction_keyword(fragment: Fragment) -> bool:
    """Check whether a fragment title contains any contradiction keyword.

    Args:
        fragment: The fragment to inspect.

    Returns:
        ``True`` if any configured keyword appears (case-insensitive).
    """
    lowered = fragment.title.lower()
    return any(kw in lowered for kw in CONTRADICTION_KEYWORDS)


@dataclass
class Paradox:
    """A detected contradiction linking two (or more) fragments.

    Attributes:
        fragment_ids: IDs of the fragments the paradox links, in stable
            order (typically two).
        contradiction_type: Which detection rule matched. One of
            ``"phase"``, ``"confidence"``, ``"dosage"``, ``"keyword"``.
        frequencies: Frequency codes shared by the fragments, used as
            additional tags on the paradox note.
        excerpts: Short excerpts (one per fragment, matching
            :attr:`fragment_ids` order).
        detected_date: The date the paradox was recorded. Defaults to
            today.
        similarity: Optional cosine similarity score between the
            fragments when embeddings are available. Stored for
            diagnostics; never surfaced to the human.
    """

    fragment_ids: list[str]
    contradiction_type: str
    frequencies: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    detected_date: date = field(default_factory=date.today)
    similarity: float = 0.0


class ParadoxDetector:
    """Detect and record paradoxes across a collection of fragments.

    Attributes:
        similarity_threshold: Minimum cosine similarity required to treat
            two fragments as sharing a topic (Rule 1 and Rule 4).
    """

    def __init__(
        self,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        """Initialise the detector.

        Args:
            similarity_threshold: Minimum cosine similarity above which
                two fragments are considered the same topic.
        """
        self.similarity_threshold = similarity_threshold

    # ---- Public API ----

    def detect_paradoxes(
        self,
        fragments: list[Fragment],
        *,
        embeddings: dict[str, list[float]] | None = None,
    ) -> list[Paradox]:
        """Scan fragments for contradictory pairs.

        Applies the four detection rules described in the module
        docstring. Each fragment pair is emitted at most once; the
        first matching rule wins in the priority order: phase, keyword,
        confidence, dosage.

        Args:
            fragments: Fragments to inspect. Fewer than two yields no
                paradoxes.
            embeddings: Optional mapping of fragment ID to embedding
                vector. When provided, it is used to compute cosine
                similarity for the topic-sharing rules. When absent,
                only the thread- and frequency-based rules can match.

        Returns:
            List of :class:`Paradox` instances, one per contradictory
            pair discovered.
        """
        if len(fragments) < 2:
            return []

        embeddings = embeddings or {}
        paradoxes: list[Paradox] = []
        for a, b in itertools.combinations(fragments, 2):
            similarity = self._pair_similarity(a, b, embeddings)
            paradox = self._detect_pair(a, b, similarity)
            if paradox is not None:
                paradoxes.append(paradox)
        return paradoxes

    def create_paradox_note(self, paradox: Paradox, vault_path: Path) -> Path:
        """Write a paradox to ``<vault>/10-Liminal/Paradoxes/`` as Markdown.

        Writes a YAML-frontmatter note that:

        * carries ``type: paradox``, the linked fragment IDs, the
          overlapping frequencies, and the detected date in frontmatter;
        * tags the note with ``paradox`` plus a lowercase tag per shared
          frequency;
        * renders each fragment as a wikilink with its excerpt;
        * states the detected tension in neutral, descriptive language;
        * includes the :data:`REFLECTION_PROMPT`.

        The note is idempotent with respect to the paradox: writing the
        same :class:`Paradox` twice produces the same file path and
        overwrites the content without creating duplicates.

        Args:
            paradox: The paradox to record.
            vault_path: Path to the root of the Obsidian vault.

        Returns:
            The path to the written markdown note.
        """
        target_dir = vault_path / "10-Liminal" / "Paradoxes"
        target_dir.mkdir(parents=True, exist_ok=True)

        tags = ["paradox"]
        tags.extend(freq.lower() for freq in paradox.frequencies)

        body = self._render_body(paradox)
        post = frontmatter.Post(
            content=body,
            type="paradox",
            fragments=paradox.fragment_ids.copy(),
            frequencies=paradox.frequencies.copy(),
            detected_date=paradox.detected_date.isoformat(),
            tags=tags,
        )

        note_path = target_dir / self._filename(paradox)
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    # ---- Pair detection ----

    def _detect_pair(
        self,
        a: Fragment,
        b: Fragment,
        similarity: float,
    ) -> Paradox | None:
        """Apply the four rules to a single fragment pair.

        Rules are evaluated in priority order: phase, keyword,
        confidence, dosage. The first match wins and short-circuits.

        Args:
            a: First fragment.
            b: Second fragment.
            similarity: Cosine similarity between their embeddings, or
                ``0.0`` when embeddings are unavailable.

        Returns:
            A Paradox for the first matching rule, or ``None``.
        """
        kind = self._match_rule(a, b, similarity)
        if kind is None:
            return None
        return self._build_paradox(a, b, kind, similarity)

    def _match_rule(
        self,
        a: Fragment,
        b: Fragment,
        similarity: float,
    ) -> str | None:
        """Return the label of the first matching detection rule."""
        high_similarity = similarity >= self.similarity_threshold
        if high_similarity and self._opposite_phases(a, b):
            return "phase"
        if self._matches_keyword_rule(a, b, high_similarity=high_similarity):
            return "keyword"
        if self._share_thread(a, b) and self._opposite_confidences(a, b):
            return "confidence"
        if self._share_primary_frequency(a, b) and self._opposite_dosages(a, b):
            return "dosage"
        return None

    def _matches_keyword_rule(
        self,
        a: Fragment,
        b: Fragment,
        *,
        high_similarity: bool,
    ) -> bool:
        """Check whether the explicit-keyword rule fires for the pair."""
        if not (_has_contradiction_keyword(a) or _has_contradiction_keyword(b)):
            return False
        return (
            high_similarity
            or self._share_thread(a, b)
            or self._share_primary_frequency(a, b)
        )

    def _pair_similarity(
        self,
        a: Fragment,
        b: Fragment,
        embeddings: dict[str, list[float]],
    ) -> float:
        """Return cosine similarity for a pair, or ``0.0`` if missing."""
        vec_a = embeddings.get(a.id)
        vec_b = embeddings.get(b.id)
        if vec_a is None or vec_b is None:
            return 0.0
        return _cosine_similarity(vec_a, vec_b)

    @staticmethod
    def _opposite_phases(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments occupy opposite wavelength phases."""
        phase_a = str(a.wavelength.phase)
        phase_b = str(b.wavelength.phase)
        if not phase_a or not phase_b:
            return False
        return frozenset({phase_a, phase_b}) in _OPPOSITE_PHASE_PAIRS

    @staticmethod
    def _opposite_confidences(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments carry opposite confidence levels."""
        conf_a = a.voice.confidence
        conf_b = b.voice.confidence
        if conf_a is None or conf_b is None:
            return False
        pair = frozenset({str(conf_a), str(conf_b)})
        return pair in _OPPOSITE_CONFIDENCE_PAIRS

    @staticmethod
    def _opposite_dosages(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments mark the same frequency medicine vs toxic."""
        dos_a = str(a.wavelength.dosage)
        dos_b = str(b.wavelength.dosage)
        return frozenset({dos_a, dos_b}) == _OPPOSITE_DOSAGE_PAIR

    @staticmethod
    def _share_thread(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments belong to at least one common thread."""
        return bool(set(a.threads) & set(b.threads))

    @staticmethod
    def _share_primary_frequency(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments share a classified primary frequency."""
        primary_a = str(a.frequency.primary)
        primary_b = str(b.frequency.primary)
        if "unclassified" in (primary_a, primary_b):
            return False
        return primary_a == primary_b

    # ---- Paradox construction ----

    @staticmethod
    def _build_paradox(
        a: Fragment,
        b: Fragment,
        kind: str,
        similarity: float,
    ) -> Paradox:
        """Assemble a :class:`Paradox` for a matched pair.

        Args:
            a: First fragment.
            b: Second fragment.
            kind: The contradiction type label.
            similarity: Similarity score (0 when embeddings absent).

        Returns:
            A populated Paradox.
        """
        shared_freqs = sorted(
            _fragment_frequencies(a) & _fragment_frequencies(b),
        )
        return Paradox(
            fragment_ids=[a.id, b.id],
            contradiction_type=kind,
            frequencies=shared_freqs,
            excerpts=[_excerpt(a), _excerpt(b)],
            similarity=similarity,
        )

    # ---- Note rendering ----

    def _render_body(self, paradox: Paradox) -> str:
        """Render the Markdown body for a paradox note."""
        lines: list[str] = []
        for idx, (fid, excerpt) in enumerate(
            zip(paradox.fragment_ids, paradox.excerpts, strict=False),
            start=1,
        ):
            lines.extend(
                (
                    f"## Fragment {idx}: [[{fid}]]",
                    "",
                    f"> {excerpt}",
                    "",
                )
            )

        lines.extend(
            (
                "## Observed Tension",
                "",
                self._describe_contradiction(paradox),
                "",
                "## Reflection Prompt",
                "",
                REFLECTION_PROMPT,
            )
        )
        return "\n".join(lines)

    @staticmethod
    def _describe_contradiction(paradox: Paradox) -> str:
        """Describe the detected tension in neutral language.

        Args:
            paradox: The paradox to describe.

        Returns:
            A one-sentence neutral description.
        """
        return _CONTRADICTION_DESCRIPTIONS.get(
            paradox.contradiction_type,
            "A tension has been detected between these fragments.",
        )

    @staticmethod
    def _filename(paradox: Paradox) -> str:
        """Build a stable filename for a paradox note."""
        iso = paradox.detected_date.isoformat()
        stem = "-".join(paradox.fragment_ids[:2]) or "paradox"
        sanitized = _FILENAME_SANITIZER.sub("-", stem).strip("-")
        return f"{iso}-{sanitized}.md"
