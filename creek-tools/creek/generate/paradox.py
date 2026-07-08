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

from creek.link.neighbours import cosine_neighbours
from creek.models import Confidence, Dosage, Frequency, Phase

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from creek.config import EmbeddingsConfig
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


_SIMILARITY_CANDIDATE_MARGIN: float = 1e-4
"""Slack subtracted from the threshold when gating similarity candidates (#790).

The bounded candidate pass uses vectorised float32 similarities only to *select*
which pairs to evaluate; the actual rule decision still runs the unchanged
:meth:`ParadoxDetector._pair_similarity` check. Widening the candidate gate by
this margin guarantees no genuinely-matching pair is dropped due to float32
rounding, so the bounded output is identical to the exhaustive scan.
"""


_EXCERPT_MAX_CHARS: int = 280
"""Maximum excerpt length written into a paradox note."""


OPPOSITE_PHASE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({Phase.RISING.value, Phase.DIMINISHING.value}),
        frozenset({Phase.RISING.value, Phase.BOTTOMING_OUT.value}),
        frozenset({Phase.PEAKING.value, Phase.BOTTOMING_OUT.value}),
        frozenset({Phase.PEAKING.value, Phase.DIMINISHING.value}),
        frozenset({Phase.WITHDRAWAL.value, Phase.RESTORATION.value}),
    },
)
"""Archetypal Wavelength phase pairs treated as opposites for Rule 1."""


OPPOSITE_CONFIDENCE_PAIRS: frozenset[frozenset[str]] = frozenset(
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


def _ordered(a: int, b: int) -> tuple[int, int]:
    """Return the pair ``(a, b)`` as a sorted ``(low, high)`` tuple.

    Args:
        a: First index.
        b: Second index.

    Returns:
        ``(a, b)`` if ``a < b`` else ``(b, a)``.
    """
    return (a, b) if a < b else (b, a)


def _within_group_pairs(groups: Iterable[list[int]]) -> set[tuple[int, int]]:
    """Return every ascending index pair drawn from within each group.

    Args:
        groups: Iterable of index lists; each list's members are paired
            with one another (across groups they are not).

    Returns:
        The set of ``(low, high)`` index pairs co-occurring in some group.
    """
    pairs: set[tuple[int, int]] = set()
    for members in groups:
        for source, other in itertools.combinations(sorted(members), 2):
            pairs.add((source, other))
    return pairs


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
        cross_level: bool = False,
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
            cross_level: FEAT-025 opt-in. When ``False`` (default),
                pairs at different structural levels (e.g. a paragraph
                vs. the section enclosing it) are skipped — that kind
                of contradiction is rhetorical structure, not a paradox.
                Set ``True`` to restore the pre-FEAT-025 behaviour and
                emit cross-level pairs as paradoxes too.

        Returns:
            List of :class:`Paradox` instances, one per contradictory
            pair discovered.
        """
        if len(fragments) < 2:
            return []

        embeddings = embeddings or {}
        if self.similarity_threshold <= 0.0:
            # Degenerate config: every pair is "high similarity", so no
            # similarity bound applies — fall back to the exhaustive scan.
            return self._detect_exhaustive(
                fragments,
                embeddings,
                cross_level=cross_level,
            )
        paradoxes: list[Paradox] = []
        for i, j in sorted(self._candidate_pairs(fragments, embeddings)):
            paradox = self._paradox_for_indices(
                fragments,
                i,
                j,
                embeddings,
                cross_level=cross_level,
            )
            if paradox is not None:
                paradoxes.append(paradox)
        return paradoxes

    def _detect_exhaustive(
        self,
        fragments: list[Fragment],
        embeddings: dict[str, list[float]],
        *,
        cross_level: bool,
    ) -> list[Paradox]:
        """All-pairs scan — the reference path, used when threshold ``<= 0``.

        Retained for the degenerate ``similarity_threshold <= 0`` config, where
        every pair counts as high-similarity so no similarity bound applies.
        O(N²); never taken on a large vault at the default threshold. The bounded
        :meth:`_candidate_pairs` path is validated to reproduce its output.

        Args:
            fragments: Fragments to scan.
            embeddings: Fragment-ID to embedding-vector mapping.
            cross_level: Whether to emit cross-structural-level pairs.

        Returns:
            One :class:`Paradox` per contradictory pair, in ascending index
            order.
        """
        paradoxes: list[Paradox] = []
        for i, j in itertools.combinations(range(len(fragments)), 2):
            paradox = self._paradox_for_indices(
                fragments,
                i,
                j,
                embeddings,
                cross_level=cross_level,
            )
            if paradox is not None:
                paradoxes.append(paradox)
        return paradoxes

    def _paradox_for_indices(
        self,
        fragments: list[Fragment],
        i: int,
        j: int,
        embeddings: dict[str, list[float]],
        *,
        cross_level: bool,
    ) -> Paradox | None:
        """Evaluate the fragment pair ``(i, j)`` under the four detection rules.

        Args:
            fragments: The fragment list.
            i: Index of the first fragment.
            j: Index of the second fragment.
            embeddings: Fragment-ID to embedding-vector mapping.
            cross_level: When ``False``, pairs at differing structural levels
                are skipped (FEAT-025).

        Returns:
            A :class:`Paradox` for the first matching rule, or ``None``.
        """
        a, b = fragments[i], fragments[j]
        if not cross_level and str(a.level) != str(b.level):
            return None
        similarity = self._pair_similarity(a, b, embeddings)
        return self._detect_pair(a, b, similarity)

    # ---- Bounded candidate generation (issue #790) ----

    def _candidate_pairs(
        self,
        fragments: list[Fragment],
        embeddings: dict[str, list[float]],
    ) -> set[tuple[int, int]]:
        """Return every index pair ``(i < j)`` that could match a rule.

        The union of four bounded families — one per rule — so it is a superset
        of the matching pairs while avoiding the O(N²) all-pairs enumeration.
        Each family is bounded: similarity by the k neighbours above threshold,
        thread/dosage/keyword by their (small) grouping cardinalities.

        Args:
            fragments: The fragment list.
            embeddings: Fragment-ID to embedding-vector mapping.

        Returns:
            The set of candidate ``(low, high)`` index pairs.
        """
        pairs = self._similarity_candidates(fragments, embeddings)
        pairs |= self._thread_candidates(fragments)
        pairs |= self._dosage_candidates(fragments)
        pairs |= self._keyword_frequency_candidates(fragments)
        return pairs

    def _similarity_candidates(
        self,
        fragments: list[Fragment],
        embeddings: dict[str, list[float]],
    ) -> set[tuple[int, int]]:
        """Pairs whose cosine similarity clears the threshold (phase/keyword rules).

        Only embedded fragments participate — a missing embedding scores ``0.0``
        similarity and so can never be "high similarity". The gate uses a small
        negative margin (:data:`_SIMILARITY_CANDIDATE_MARGIN`) so float32
        rounding never drops a pair the exact per-pair check would keep.

        Args:
            fragments: The fragment list.
            embeddings: Fragment-ID to embedding-vector mapping.

        Returns:
            The set of high-similarity ``(low, high)`` index pairs.
        """
        indexed = [
            (idx, embeddings[frag.id])
            for idx, frag in enumerate(fragments)
            if frag.id in embeddings
        ]
        if not indexed:
            return set()
        global_indices = [idx for idx, _ in indexed]
        neighbours = cosine_neighbours(
            [vec for _, vec in indexed],
            self.similarity_threshold - _SIMILARITY_CANDIDATE_MARGIN,
        )
        pairs: set[tuple[int, int]] = set()
        for local_index, local_neighbours in enumerate(neighbours):
            source = global_indices[local_index]
            pairs.update(
                _ordered(source, global_indices[local_other])
                for local_other in local_neighbours
            )
        return pairs

    @staticmethod
    def _thread_candidates(fragments: list[Fragment]) -> set[tuple[int, int]]:
        """Pairs sharing at least one thread (confidence and keyword rules).

        Args:
            fragments: The fragment list.

        Returns:
            The set of shared-thread ``(low, high)`` index pairs.
        """
        by_thread: dict[str, list[int]] = {}
        for idx, frag in enumerate(fragments):
            for thread in set(frag.threads):
                by_thread.setdefault(thread, []).append(idx)
        return _within_group_pairs(by_thread.values())

    @staticmethod
    def _dosage_candidates(fragments: list[Fragment]) -> set[tuple[int, int]]:
        """Same-primary-frequency medicine/toxic pairs (dosage rule).

        Only the cross product of the (typically small) medicine and toxic sets
        within each primary frequency is emitted — never the whole frequency
        group, which can hold thousands of fragments.

        Args:
            fragments: The fragment list.

        Returns:
            The set of medicine/toxic ``(low, high)`` index pairs.
        """
        medicine: dict[str, list[int]] = {}
        toxic: dict[str, list[int]] = {}
        for idx, frag in enumerate(fragments):
            primary = str(frag.frequency.primary)
            if primary == Frequency.UNCLASSIFIED.value:
                continue
            dosage = str(frag.wavelength.dosage)
            if dosage == Dosage.MEDICINE.value:
                medicine.setdefault(primary, []).append(idx)
            elif dosage == Dosage.TOXIC.value:
                toxic.setdefault(primary, []).append(idx)
        pairs: set[tuple[int, int]] = set()
        for primary, med_indices in medicine.items():
            for source in med_indices:
                pairs.update(
                    _ordered(source, other) for other in toxic.get(primary, [])
                )
        return pairs

    @staticmethod
    def _keyword_frequency_candidates(
        fragments: list[Fragment],
    ) -> set[tuple[int, int]]:
        """Keyword-bearing fragment x same-primary-frequency fragment (keyword rule).

        The keyword rule also fires via high similarity or a shared thread, but
        those pairs are already covered by the similarity and thread families —
        so only the shared-primary-frequency arm is added here. Bounded by the
        (rare) count of keyword-bearing fragments.

        Args:
            fragments: The fragment list.

        Returns:
            The set of keyword x same-frequency ``(low, high)`` index pairs.
        """
        keyword_indices = [
            idx
            for idx, frag in enumerate(fragments)
            if _has_contradiction_keyword(frag)
        ]
        if not keyword_indices:
            return set()
        by_primary: dict[str, list[int]] = {}
        for idx, frag in enumerate(fragments):
            primary = str(frag.frequency.primary)
            if primary != Frequency.UNCLASSIFIED.value:
                by_primary.setdefault(primary, []).append(idx)
        pairs: set[tuple[int, int]] = set()
        for source in keyword_indices:
            primary = str(fragments[source].frequency.primary)
            for other in by_primary.get(primary, []):
                if other != source:
                    pairs.add(_ordered(source, other))
        return pairs

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
        return frozenset({phase_a, phase_b}) in OPPOSITE_PHASE_PAIRS

    @staticmethod
    def _opposite_confidences(a: Fragment, b: Fragment) -> bool:
        """Check whether two fragments carry opposite confidence levels."""
        conf_a = a.voice.confidence
        conf_b = b.voice.confidence
        if conf_a is None or conf_b is None:
            return False
        pair = frozenset({str(conf_a), str(conf_b)})
        return pair in OPPOSITE_CONFIDENCE_PAIRS

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


def generate_paradoxes(
    vault_path: Path,
    embeddings_config: EmbeddingsConfig,
) -> list[Path]:
    """Detect contradictory fragment pairs and write paradox notes (#711).

    The runnable wiring for ``creek report --type paradox``: loads the vault's
    fragments and their cached embeddings, runs :class:`ParadoxDetector`, and
    writes one note per paradox into ``10-Liminal/Paradoxes/``. Idempotent — each
    note's path is stable per paradox, so re-running overwrites in place rather
    than duplicating. Embeddings sharpen the topic-sharing rules; when the cache
    is empty only the thread/frequency rules can match (still useful, no crash).

    Args:
        vault_path: Root of the Obsidian vault.
        embeddings_config: Embeddings config (model + cache) used to load the
            per-vault vector cache for the topic-similarity rules.

    Returns:
        Paths of the paradox notes written this run (empty when none are found).
    """
    from creek.link.embeddings import EmbeddingLinker, embeddings_cache_path
    from creek.vault.reader import iter_vault_fragments

    fragments = [
        fragment
        for _path, fragment, _body, _raw in iter_vault_fragments(
            vault_path / "01-Fragments",
        )
    ]
    cache = EmbeddingLinker(embeddings_config).load_cache(
        embeddings_cache_path(vault_path),
    )
    embeddings = {fid: cached.vector for fid, cached in cache.items()}
    detector = ParadoxDetector()
    return [
        detector.create_paradox_note(paradox, vault_path)
        for paradox in detector.detect_paradoxes(fragments, embeddings=embeddings)
    ]
