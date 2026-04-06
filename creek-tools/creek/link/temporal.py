"""Temporal proximity linker — find fragments created near each other in time.

Provides ``TemporalLink`` (a Pydantic model for scored temporal links) and
``TemporalLinker`` which groups fragments by configurable time windows,
identifies cross-source pairs, and scores overlap across multiple
classification dimensions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from creek.models import Fragment

logger = logging.getLogger(__name__)

_UNCLASSIFIED = "unclassified"


class TemporalLink(BaseModel):
    """A scored temporal proximity link between two fragments.

    Attributes:
        fragment_a_id: ID of the first fragment in the pair.
        fragment_b_id: ID of the second fragment in the pair.
        time_delta_hours: Hours between the two fragments' creation times.
        overlap_score: Combined overlap score across all dimensions.
        shared_dimensions: Names of dimensions that contributed to the score.
    """

    fragment_a_id: str
    fragment_b_id: str
    time_delta_hours: float
    overlap_score: float
    shared_dimensions: list[str]


def _score_pair(frag_a: Fragment, frag_b: Fragment) -> tuple[float, list[str]]:
    """Compute the overlap score and shared dimensions for a fragment pair.

    Scoring breakdown:
    - Same primary frequency (non-unclassified): +0.3
    - Each shared secondary frequency: +0.1
    - Same wavelength phase (non-unclassified): +0.2
    - Same mode (non-unclassified): +0.1
    - Each shared emotional_texture tag: +0.1
    - Different source platforms: +0.2

    Args:
        frag_a: First fragment.
        frag_b: Second fragment.

    Returns:
        A tuple of (score, shared_dimensions).
    """
    score = 0.0
    dimensions: list[str] = []

    # Same primary frequency
    primary_a = frag_a.frequency.primary
    primary_b = frag_b.frequency.primary
    if primary_a == primary_b and primary_a != _UNCLASSIFIED:
        score += 0.3
        dimensions.append("primary_frequency")

    # Shared secondary frequencies
    sec_a = set(frag_a.frequency.secondary)
    sec_b = set(frag_b.frequency.secondary)
    shared_sec = sec_a & sec_b
    if shared_sec:
        score += 0.1 * len(shared_sec)
        dimensions.append("secondary_frequency")

    # Same wavelength phase
    phase_a = frag_a.wavelength.phase
    phase_b = frag_b.wavelength.phase
    if phase_a == phase_b and phase_a != _UNCLASSIFIED:
        score += 0.2
        dimensions.append("wavelength_phase")

    # Same mode
    mode_a = frag_a.wavelength.mode
    mode_b = frag_b.wavelength.mode
    if mode_a == mode_b and mode_a != _UNCLASSIFIED:
        score += 0.1
        dimensions.append("mode")

    # Shared emotional texture tags
    tex_a = set(frag_a.emotional_texture)
    tex_b = set(frag_b.emotional_texture)
    shared_tex = tex_a & tex_b
    if shared_tex:
        score += 0.1 * len(shared_tex)
        dimensions.append("emotional_texture")

    # Different source platforms bonus
    if frag_a.source.platform != frag_b.source.platform:
        score += 0.2
        dimensions.append("cross_source")

    return score, dimensions


class TemporalLinker:
    """Find temporal proximity links between fragments from different sources.

    Sorts fragments chronologically, then compares each pair within the
    configured time window.  Only cross-source pairs are considered.
    Each pair is scored on multiple classification dimensions and filtered
    by a minimum score threshold.

    Attributes:
        min_score: Minimum overlap score for a link to be included.
    """

    def __init__(self, min_score: float = 0.3) -> None:
        """Initialise the TemporalLinker with a minimum score threshold.

        Args:
            min_score: Minimum combined overlap score for a link to be
                returned.  Defaults to 0.3.
        """
        self.min_score = min_score

    def find_temporal_links(
        self, fragments: list[Fragment], window_hours: int
    ) -> list[TemporalLink]:
        """Find fragment pairs created within a time window with thematic overlap.

        Fragments are sorted chronologically, then each pair from different
        sources within the window is scored across frequency, wavelength,
        mode, emotional texture, and source dimensions.

        Args:
            fragments: List of fragments to check for temporal proximity.
            window_hours: Maximum hours between creation times to consider
                fragments temporally linked.

        Returns:
            A list of ``TemporalLink`` objects for each qualifying pair,
            sorted by overlap score descending.
        """
        logger.info(
            "Finding temporal links among %d fragment(s) "
            "within %d-hour window (min_score=%.2f)",
            len(fragments),
            window_hours,
            self.min_score,
        )

        min_pair_size = 2
        if len(fragments) < min_pair_size:
            return []

        sorted_frags = sorted(fragments, key=lambda f: f.created)
        links: list[TemporalLink] = []

        for i, frag_a in enumerate(sorted_frags):
            for frag_b in sorted_frags[i + 1 :]:
                delta = frag_b.created - frag_a.created
                delta_hours = delta.total_seconds() / 3600.0

                if delta_hours > window_hours:
                    break

                # Only consider cross-source pairs
                if frag_a.source.platform == frag_b.source.platform:
                    continue

                score, dimensions = _score_pair(frag_a, frag_b)

                if score >= self.min_score:
                    links.append(
                        TemporalLink(
                            fragment_a_id=frag_a.id,
                            fragment_b_id=frag_b.id,
                            time_delta_hours=delta_hours,
                            overlap_score=round(score, 10),
                            shared_dimensions=dimensions,
                        ),
                    )

        links.sort(key=lambda lnk: lnk.overlap_score, reverse=True)

        logger.info("Found %d temporal link(s)", len(links))
        return links
