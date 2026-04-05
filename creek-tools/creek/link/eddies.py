"""Eddy detection for Creek fragments.

Provides the ``EddyDetector`` class which identifies topic cluster
eddies — convergence points where fragments sharing multiple tags or
emotional textures cluster together.  Eddies represent areas of
concentrated attention and meaning in the knowledge graph.
"""

import logging
from collections import defaultdict

from creek.config import LinkingConfig
from creek.models import Eddy, Fragment

logger = logging.getLogger(__name__)


class EddyDetector:
    """Detect topic cluster eddies across a collection of fragments.

    Analyses fragment tags and emotional textures to find clusters of
    fragments that share significant overlap, then produces Eddy objects
    for clusters meeting the minimum fragment threshold.

    Attributes:
        config: Linking configuration with eddy_min_fragments threshold.
    """

    def __init__(self, config: LinkingConfig | None = None) -> None:
        """Initialise the EddyDetector with optional configuration.

        Args:
            config: Linking configuration.  If ``None``, uses defaults.
        """
        self.config = config or LinkingConfig()

    def detect_eddies(self, fragments: list[Fragment]) -> list[Eddy]:
        """Detect topic cluster eddies in a set of fragments.

        Groups fragments by shared tags and creates an ``Eddy`` for
        each tag that appears in at least ``eddy_min_fragments``
        fragments.

        Args:
            fragments: List of fragments to scan for eddy patterns.

        Returns:
            A list of detected ``Eddy`` objects, sorted by fragment
            count descending.
        """
        if not fragments:
            logger.info("No fragments provided — skipping eddy detection")
            return []

        tag_groups: dict[str, list[Fragment]] = defaultdict(list)
        for fragment in fragments:
            for tag in fragment.tags:
                tag_groups[tag].append(fragment)

        min_frags = self.config.eddy_min_fragments
        eddies: list[Eddy] = []

        for tag, group_frags in sorted(tag_groups.items()):
            if len(group_frags) < min_frags:
                continue

            dates = [f.created.date() for f in group_frags]
            thread_ids: list[str] = []
            for frag in group_frags:
                for tid in frag.threads:
                    if tid not in thread_ids:
                        thread_ids.append(tid)

            eddy = Eddy(
                title=f"Eddy: {tag}",
                formed=min(dates),
                fragment_count=len(group_frags),
                threads=thread_ids,
                description=(
                    f"Topic cluster around '{tag}' ({len(group_frags)} fragments)"
                ),
            )
            eddies.append(eddy)

        eddies.sort(key=lambda e: e.fragment_count, reverse=True)

        logger.info(
            "Detected %d eddy/eddies among %d fragment(s) (min_fragments=%d)",
            len(eddies),
            len(fragments),
            min_frags,
        )
        return eddies
