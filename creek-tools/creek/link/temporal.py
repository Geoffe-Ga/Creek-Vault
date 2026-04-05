"""Temporal proximity linker for Creek fragments.

Provides the ``TemporalLinker`` class which finds fragments created
within a configurable time window of each other, producing temporal
proximity links that strengthen resonance signals.
"""

import logging

from creek.models import Fragment

logger = logging.getLogger(__name__)


class TemporalLinker:
    """Find temporal proximity links between fragments.

    Compares creation timestamps of all fragment pairs and returns
    those within the configured time window as temporal links.
    """

    def find_temporal_links(
        self, fragments: list[Fragment], window_hours: int
    ) -> list[tuple[str, str]]:
        """Find fragment pairs created within a time window of each other.

        Compares the ``created`` timestamp of each fragment pair and
        returns pairs whose absolute time difference is within
        *window_hours*.

        Args:
            fragments: List of fragments to check for temporal proximity.
            window_hours: Maximum hours between creation times to
                consider fragments temporally linked.

        Returns:
            A list of ``(fragment_id_a, fragment_id_b)`` tuples for each
            temporal link found, sorted by time proximity (closest first).
        """
        if len(fragments) < 2:
            logger.info(
                "Fewer than 2 fragments — skipping temporal linking",
            )
            return []

        window_seconds = window_hours * 3600
        links: list[tuple[str, str, float]] = []

        sorted_frags = sorted(fragments, key=lambda f: f.created)

        for i, frag_a in enumerate(sorted_frags):
            for frag_b in sorted_frags[i + 1 :]:
                delta = abs((frag_b.created - frag_a.created).total_seconds())
                if delta <= window_seconds:
                    links.append((frag_a.id, frag_b.id, delta))
                else:
                    break

        links.sort(key=lambda t: t[2])
        result = [(a, b) for a, b, _ in links]

        logger.info(
            "Found %d temporal link(s) among %d fragment(s) within %d-hour window",
            len(result),
            len(fragments),
            window_hours,
        )
        return result
