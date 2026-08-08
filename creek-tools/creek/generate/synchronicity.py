"""Synchronicity detection — flag surprising cross-source resonances.

Section 10.3 of the Creek Ontology describes *synchronicities* as
meaningful coincidences: fragments from very different sources and times
that arrive at near-identical meaning.  The linking pass already surfaces
resonance pairs via cosine similarity; this module filters those
resonances against four criteria and, when a pair qualifies, writes a
reflection note to ``10-Liminal/Synchronicities/``.

The criteria are deliberately strict:

1. Semantic similarity strictly greater than 0.9.
2. The two fragments come from *different* source platforms.
3. Their authored timestamps are separated by more than 30 days
   (FEAT-031: routed through :func:`creek.time.effective_authored_at`
   so a multi-year corpus ingested in one batch still surfaces
   cross-year pairs).
4. The pair is not obviously about the same project/task (status-update
   phrases and shared proper-noun project names are filtered out).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import frontmatter

from creek.link.embeddings import Resonance
from creek.models import Synchronicity
from creek.time import effective_authored_at

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import EmbeddingsConfig
    from creek.models import Fragment, FragmentLevel

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD: float = 0.9
"""Cosine similarity must exceed this value to qualify as a synchronicity."""

DEFAULT_MIN_TIME_GAP_DAYS: int = 30
"""Fragments must be separated by more than this many days."""

_STATUS_UPDATE_PHRASES: tuple[str, ...] = (
    "still working on",
    "progress on",
    "update on",
)
"""Phrases that mark a fragment as a status update rather than a synchronicity."""

_PROPER_NOUN_PATTERN = re.compile(r"(?<!^)\b([A-Z][a-zA-Z0-9]{2,})\b")
"""Match proper-noun-like tokens that are not at the start of the string."""

_REFLECTION_PROMPT = (
    "These fragments emerged independently but echo each other. "
    "What pattern is trying to surface?"
)


def _extract_proper_nouns(title: str) -> set[str]:
    """Return proper-noun-like tokens from *title*.

    A proper noun is approximated as a capitalised word of three or more
    characters that does not sit at the start of the string.  The
    sentence-initial guard prevents ordinary titles (``"Forgiveness keeps
    arriving"``) from being treated as shared project names.

    Args:
        title: The fragment title to scan.

    Returns:
        A set of candidate proper-noun tokens in their original casing.
    """
    return {match.group(1) for match in _PROPER_NOUN_PATTERN.finditer(title)}


def _has_status_update_phrase(title: str) -> bool:
    """Return ``True`` if *title* contains a status-update phrase."""
    lowered = title.lower()
    return any(phrase in lowered for phrase in _STATUS_UPDATE_PHRASES)


def _share_project(title_a: str, title_b: str) -> bool:
    """Return ``True`` if the two titles share a proper-noun project name."""
    return bool(_extract_proper_nouns(title_a) & _extract_proper_nouns(title_b))


_SYNCHRONICITY_SUBPATH: tuple[str, str] = ("10-Liminal", "Synchronicities")
"""Vault-relative home of the reflection notes this module writes.

Named once so the scaffold drift guard can derive it instead of
re-typing it (#1025)."""

_LEGACY_TUPLE_ARITY: int = 3
"""Pre-FEAT-024 resonance tuple shape: (id_a, id_b, similarity)."""

_PAIR_SIZE: int = 2
"""A synchronicity links exactly two fragments (its idempotency key)."""


def _normalise_resonance(
    resonance: Resonance | tuple[str, str, float],
) -> tuple[str, str, float, FragmentLevel, FragmentLevel] | None:
    """Unpack either resonance flavour into a uniform 5-tuple.

    Accepting both the canonical :class:`Resonance` model and the
    pre-FEAT-024 ``(id_a, id_b, similarity)`` tuple keeps existing
    callers (and historical fixtures in test suites) working through
    the transition. Any other shape is logged and returned as ``None``
    so the detection loop can skip it without raising.

    Args:
        resonance: A :class:`Resonance` or a legacy 3-tuple.

    Returns:
        ``(fragment_a_id, fragment_b_id, similarity, from_level,
        to_level)`` for recognised shapes; ``None`` otherwise.
    """
    if isinstance(resonance, Resonance):
        # ``model_config`` is set to ``use_enum_values=False`` by default,
        # so the level fields are already plain ``FragmentLevel`` strings.
        return (
            resonance.fragment_a_id,
            resonance.fragment_b_id,
            resonance.similarity,
            resonance.from_level,
            resonance.to_level,
        )
    if isinstance(resonance, tuple) and len(resonance) == _LEGACY_TUPLE_ARITY:
        frag_a_id, frag_b_id, similarity = resonance
        return (frag_a_id, frag_b_id, similarity, "document", "document")
    logger.warning(
        "Unrecognised resonance shape %r; skipping. Pass a Resonance or "
        "a (id_a, id_b, similarity) tuple.",
        type(resonance).__name__,
    )
    return None


class SynchronicityDetector:
    """Filter resonances down to meaningful cross-source synchronicities.

    Attributes:
        similarity_threshold: Minimum cosine similarity (strictly greater
            than) required.  Defaults to :data:`DEFAULT_SIMILARITY_THRESHOLD`.
        min_time_gap_days: Minimum number of days between the two
            fragments' creation timestamps (strictly greater than).
            Defaults to :data:`DEFAULT_MIN_TIME_GAP_DAYS`.
    """

    def __init__(
        self,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        min_time_gap_days: int = DEFAULT_MIN_TIME_GAP_DAYS,
    ) -> None:
        """Initialise the detector with optional threshold overrides.

        Args:
            similarity_threshold: Minimum cosine similarity (exclusive).
            min_time_gap_days: Minimum creation-time gap in days (exclusive).
        """
        self.similarity_threshold = similarity_threshold
        self.min_time_gap_days = min_time_gap_days

    def detect_synchronicities(
        self,
        resonances: list[Resonance] | list[tuple[str, str, float]],
        fragments: dict[str, Fragment],
    ) -> list[Synchronicity]:
        """Filter *resonances* into :class:`Synchronicity` records.

        FEAT-024 ranks cross-level pairs (``level_a != level_b``) ahead
        of same-level pairs in the returned list — when a sentence-level
        child resonates with an exchange-level fragment from a different
        source, that asymmetry is itself a signal worth surfacing
        deliberately rather than burying among same-level matches.

        Args:
            resonances: :class:`Resonance` records (canonical) or the
                legacy ``(fragment_id_a, fragment_id_b, similarity)``
                tuple form, both emitted by
                :class:`~creek.link.embeddings.EmbeddingLinker` across
                FEAT-024's transition. Tuples carry no level metadata,
                so synchronicities derived from them keep the default
                ``"document"`` levels.
            fragments: Mapping of fragment ID to :class:`Fragment` used
                to inspect metadata.  Resonances referencing unknown IDs
                are silently skipped.

        Returns:
            A list of :class:`Synchronicity` objects, one per qualifying
            resonance, sorted with cross-level pairs first (highest
            similarity within each group).
        """
        results: list[Synchronicity] = []
        for resonance in resonances:
            normalised = _normalise_resonance(resonance)
            if normalised is None:
                continue
            frag_a_id, frag_b_id, similarity, from_level, to_level = normalised
            frag_a = fragments.get(frag_a_id)
            frag_b = fragments.get(frag_b_id)
            if frag_a is None or frag_b is None:
                continue
            sync = self._evaluate_pair(
                frag_a,
                frag_b,
                similarity,
                from_level=from_level,
                to_level=to_level,
            )
            if sync is not None:
                results.append(sync)
        # Cross-level pairs (level_a != level_b) sort first; within each
        # group, highest similarity wins. The boolean sort key works
        # because Python sorts ``False`` before ``True``.
        results.sort(key=lambda s: (s.level_a == s.level_b, -s.similarity))
        return results

    def _evaluate_pair(
        self,
        frag_a: Fragment,
        frag_b: Fragment,
        similarity: float,
        *,
        from_level: FragmentLevel,
        to_level: FragmentLevel,
    ) -> Synchronicity | None:
        """Return a :class:`Synchronicity` if the pair passes every criterion.

        Args:
            frag_a: First fragment in the resonance pair.
            frag_b: Second fragment in the resonance pair.
            similarity: Cosine similarity score for the pair.
            from_level: Structural level recorded on the resonance for
                ``frag_a`` (defaults to ``"document"`` for legacy tuples).
            to_level: Structural level recorded on the resonance for
                ``frag_b``.

        Returns:
            A :class:`Synchronicity` record, or ``None`` if any criterion
            fails.
        """
        if similarity <= self.similarity_threshold:
            return None
        if frag_a.source.platform == frag_b.source.platform:
            return None

        earlier, later, level_earlier, level_later = self._chronological_pair(
            frag_a,
            frag_b,
            from_level=from_level,
            to_level=to_level,
        )
        # FEAT-031: gap is measured against the authored-date
        # precedence so cross-year synchronicities survive batch
        # ingestion (which would otherwise collapse ``created`` to a
        # single wall-clock moment and reduce the gap to zero).
        gap_days = (effective_authored_at(later) - effective_authored_at(earlier)).days
        if gap_days <= self.min_time_gap_days:
            return None

        if _has_status_update_phrase(earlier.title) or _has_status_update_phrase(
            later.title,
        ):
            return None
        if _share_project(earlier.title, later.title):
            return None

        return Synchronicity(
            fragment_a_id=earlier.id,
            fragment_b_id=later.id,
            similarity=similarity,
            time_gap_days=gap_days,
            source_a=earlier.source.platform,
            source_b=later.source.platform,
            level_a=level_earlier,
            level_b=level_later,
        )

    @staticmethod
    def _chronological_pair(
        frag_a: Fragment,
        frag_b: Fragment,
        *,
        from_level: FragmentLevel,
        to_level: FragmentLevel,
    ) -> tuple[Fragment, Fragment, FragmentLevel, FragmentLevel]:
        """Return the pair ordered ``(earlier, later, lvl_earlier, lvl_later)``.

        Ordering by creation time matches what the synchronicity record
        promises (``fragment_a_id`` is always the earlier of the two);
        the levels follow the same flip so callers can attribute each
        level to the right endpoint without re-checking timestamps.

        Args:
            frag_a: First fragment in the resonance pair.
            frag_b: Second fragment in the resonance pair.
            from_level: Resonance-recorded level of ``frag_a``.
            to_level: Resonance-recorded level of ``frag_b``.

        Returns:
            ``(earlier, later, level_earlier, level_later)``.
        """
        # FEAT-031: order by the authored-date precedence so the
        # "earlier" fragment is the one actually authored first, even
        # when both share an ingest-time ``created`` wall-clock.
        if effective_authored_at(frag_a) <= effective_authored_at(frag_b):
            return frag_a, frag_b, from_level, to_level
        return frag_b, frag_a, to_level, from_level

    def create_synchronicity_note(
        self,
        sync: Synchronicity,
        fragments: dict[str, Fragment],
        vault_path: Path,
    ) -> Path:
        """Write a synchronicity reflection note to the vault.

        The note lives at ``{vault_path}/10-Liminal/Synchronicities/`` and
        carries YAML frontmatter describing the pair plus a body with
        fragment excerpts, the similarity score, the time gap, and the
        reflection prompt from Section 10.3 of the ontology.

        Args:
            sync: The synchronicity to serialise.
            fragments: Mapping that must contain both referenced fragments.
            vault_path: Root of the Obsidian vault.

        Returns:
            Path to the written markdown file.

        Raises:
            KeyError: If either referenced fragment is missing from
                *fragments*.
        """
        frag_a = fragments[sync.fragment_a_id]
        frag_b = fragments[sync.fragment_b_id]

        target_dir = vault_path.joinpath(*_SYNCHRONICITY_SUBPATH)
        target_dir.mkdir(parents=True, exist_ok=True)
        note_path = target_dir / f"{sync.id}.md"

        body = self._render_body(sync, frag_a, frag_b)

        post = frontmatter.Post(
            content=body,
            type="synchronicity",
            id=sync.id,
            fragments=[f"[[{frag_a.id}]]", f"[[{frag_b.id}]]"],
            similarity=round(sync.similarity, 4),
            time_gap_days=sync.time_gap_days,
            sources=[str(sync.source_a), str(sync.source_b)],
            tags=sync.tags.copy(),
        )
        note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        return note_path

    @staticmethod
    def _render_body(
        sync: Synchronicity,
        frag_a: Fragment,
        frag_b: Fragment,
    ) -> str:
        """Render the markdown body for a synchronicity note.

        Args:
            sync: The synchronicity being documented.
            frag_a: Earlier fragment in the pair.
            frag_b: Later fragment in the pair.

        Returns:
            A markdown string with fragment excerpts, numeric details,
            the reflection prompt, and the ``#synchronicity`` tag.
        """
        # FEAT-031: render the authored-date precedence so the body
        # answers "when was this written?" rather than "when did Creek
        # see it?".
        date_a = effective_authored_at(frag_a).date().isoformat()
        date_b = effective_authored_at(frag_b).date().isoformat()
        lines = [
            "## Fragment excerpts",
            "",
            f"- **{frag_a.source.platform}** ({date_a}): {frag_a.title}",
            f"- **{frag_b.source.platform}** ({date_b}): {frag_b.title}",
            "",
            "## Details",
            "",
            f"- Similarity: {sync.similarity:.4f}",
            f"- Time gap: {sync.time_gap_days} days",
            "",
            "## Reflection prompt",
            "",
            _REFLECTION_PROMPT,
            "",
            "#synchronicity",
        ]
        return "\n".join(lines)


def generate_synchronicities(
    vault_path: Path,
    embeddings_config: EmbeddingsConfig,
) -> list[Path]:
    """Surface surprising cross-source resonances as Synchronicity notes (#711).

    The runnable wiring for ``creek report --type synchronicity``: loads the
    vault's fragments and their cached embeddings, computes resonance pairs via
    :class:`~creek.link.embeddings.EmbeddingLinker`, filters them through
    :class:`SynchronicityDetector`, and writes one note per qualifying pair into
    ``10-Liminal/Synchronicities/``. **Idempotent**: a fragment pair already
    captured by an existing note is skipped, so re-running never duplicates —
    necessary because :attr:`Synchronicity.id` is a fresh ``uuid`` per record,
    so the filename alone is not stable across runs. An empty embeddings cache
    yields no resonances and therefore no notes (no crash).

    Args:
        vault_path: Root of the Obsidian vault.
        embeddings_config: Embeddings config (model + cache) used to load the
            per-vault vector cache and resolve the similarity threshold.

    Returns:
        Paths of the synchronicity notes written this run (empty when none).
    """
    from creek.link.embeddings import EmbeddingLinker, embeddings_cache_path
    from creek.vault.reader import iter_vault_fragments

    fragments = {
        fragment.id: fragment
        for _path, fragment, _body, _raw in iter_vault_fragments(
            vault_path / "01-Fragments",
        )
    }
    linker = EmbeddingLinker(embeddings_config)
    cache = linker.load_cache(embeddings_cache_path(vault_path))
    embeddings = {fid: cached.vector for fid, cached in cache.items()}
    resonances = linker.find_resonances(embeddings, fragments) if embeddings else []
    detector = SynchronicityDetector()
    already = _existing_synchronicity_pairs(vault_path)
    written: list[Path] = []
    for sync in detector.detect_synchronicities(resonances, fragments):
        pair = frozenset({sync.fragment_a_id, sync.fragment_b_id})
        if pair in already:
            continue
        written.append(detector.create_synchronicity_note(sync, fragments, vault_path))
        already.add(pair)
    return written


def _existing_synchronicity_pairs(vault_path: Path) -> set[frozenset[str]]:
    """Return the fragment pairs already captured by synchronicity notes.

    Reads each note's ``fragments: ["[[a]]", "[[b]]"]`` frontmatter so a re-run
    can skip pairs already written — the idempotency key, since the note's
    ``sync-<uuid>`` filename is not stable across runs.
    """
    pairs: set[frozenset[str]] = set()
    sync_dir = vault_path.joinpath(*_SYNCHRONICITY_SUBPATH)
    if not sync_dir.is_dir():
        return pairs
    for note in sync_dir.glob("*.md"):
        try:
            post = frontmatter.loads(note.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raw = post.get("fragments")
        if not isinstance(raw, list) or len(raw) < _PAIR_SIZE:
            continue
        ids = [re.sub(r"[\[\]]", "", str(item)).strip() for item in raw[:_PAIR_SIZE]]
        pairs.add(frozenset(ids))
    return pairs
