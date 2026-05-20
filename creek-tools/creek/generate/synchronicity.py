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
3. Their creation timestamps are separated by more than 30 days.
4. The pair is not obviously about the same project/task (status-update
   phrases and shared proper-noun project names are filtered out).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import frontmatter

from creek.models import Synchronicity

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment

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
        resonances: list[tuple[str, str, float]],
        fragments: dict[str, Fragment],
    ) -> list[Synchronicity]:
        """Filter *resonances* into :class:`Synchronicity` records.

        Args:
            resonances: Tuples of ``(fragment_id_a, fragment_id_b,
                similarity)`` as emitted by
                :class:`~creek.link.embeddings.EmbeddingLinker`.
            fragments: Mapping of fragment ID to :class:`Fragment` used
                to inspect metadata.  Resonances referencing unknown IDs
                are silently skipped.

        Returns:
            A list of :class:`Synchronicity` objects, one per qualifying
            resonance, with the earlier fragment stored in
            ``fragment_a_id``.
        """
        results: list[Synchronicity] = []
        for frag_a_id, frag_b_id, similarity in resonances:
            frag_a = fragments.get(frag_a_id)
            frag_b = fragments.get(frag_b_id)
            if frag_a is None or frag_b is None:
                continue
            sync = self._evaluate_pair(frag_a, frag_b, similarity)
            if sync is not None:
                results.append(sync)
        return results

    def _evaluate_pair(
        self,
        frag_a: Fragment,
        frag_b: Fragment,
        similarity: float,
    ) -> Synchronicity | None:
        """Return a :class:`Synchronicity` if the pair passes every criterion.

        Args:
            frag_a: First fragment in the resonance pair.
            frag_b: Second fragment in the resonance pair.
            similarity: Cosine similarity score for the pair.

        Returns:
            A :class:`Synchronicity` record, or ``None`` if any criterion
            fails.
        """
        if similarity <= self.similarity_threshold:
            return None
        if frag_a.source.platform == frag_b.source.platform:
            return None

        earlier, later = self._chronological_pair(frag_a, frag_b)
        gap_days = (later.created - earlier.created).days
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
        )

    @staticmethod
    def _chronological_pair(
        frag_a: Fragment,
        frag_b: Fragment,
    ) -> tuple[Fragment, Fragment]:
        """Return the pair ordered ``(earlier, later)`` by creation time."""
        if frag_a.created <= frag_b.created:
            return frag_a, frag_b
        return frag_b, frag_a

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

        target_dir = vault_path / "10-Liminal" / "Synchronicities"
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
        lines = [
            "## Fragment excerpts",
            "",
            f"- **{frag_a.source.platform}** ({frag_a.created.date().isoformat()}): "
            f"{frag_a.title}",
            f"- **{frag_b.source.platform}** ({frag_b.created.date().isoformat()}): "
            f"{frag_b.title}",
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
