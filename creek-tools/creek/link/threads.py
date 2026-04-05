"""Thread detection for Creek fragments.

Provides the ``ThreadDetector`` class which identifies recurring
narrative threads by clustering fragments that share primary frequency
classifications.  Threads represent narrative currents — recurring
themes or patterns that evolve over time.
"""

import logging
from collections import defaultdict

from creek.config import LinkingConfig
from creek.models import Fragment, Frequency, Thread

logger = logging.getLogger(__name__)

_FREQUENCY_LABELS: dict[str, str] = {
    "F1": "Survival & Safety",
    "F2": "Connection & Belonging",
    "F3": "Power & Agency",
    "F4": "Order & Structure",
    "F5": "Achievement & Expression",
    "F6": "Community & Care",
    "F7": "Systems & Integration",
    "F8": "Holistic & Ecological",
    "F9": "Cosmic & Transcendent",
    "F10": "Unity & Non-Dual",
}


class ThreadDetector:
    """Detect narrative threads across a collection of fragments.

    Clusters fragments by their primary frequency classification and
    creates Thread objects for clusters meeting the minimum fragment
    threshold.

    Attributes:
        config: Linking configuration with thread_min_fragments threshold.
    """

    def __init__(self, config: LinkingConfig | None = None) -> None:
        """Initialise the ThreadDetector with optional configuration.

        Args:
            config: Linking configuration.  If ``None``, uses defaults.
        """
        self.config = config or LinkingConfig()

    def detect_threads(self, fragments: list[Fragment]) -> list[Thread]:
        """Detect recurring narrative threads in a set of fragments.

        Groups fragments by their primary frequency and creates a
        ``Thread`` for each group meeting the minimum fragment count.

        Args:
            fragments: List of fragments to scan for thread patterns.

        Returns:
            A list of detected ``Thread`` objects, sorted by fragment
            count descending.
        """
        if not fragments:
            logger.info("No fragments provided — skipping thread detection")
            return []

        clusters: dict[str, list[Fragment]] = defaultdict(list)
        for fragment in fragments:
            primary = fragment.frequency.primary
            if primary != Frequency.UNCLASSIFIED:
                clusters[primary].append(fragment)

        threads: list[Thread] = []
        min_frags = self.config.thread_min_fragments

        for freq_key, cluster_frags in sorted(clusters.items()):
            if len(cluster_frags) < min_frags:
                continue

            dates = [f.created.date() for f in cluster_frags]
            label = _FREQUENCY_LABELS.get(freq_key, freq_key)

            thread = Thread(
                title=f"Thread: {label}",
                status="active",
                first_seen=min(dates),
                last_seen=max(dates),
                frequency_affinity=[freq_key],
                fragment_count=len(cluster_frags),
                description=(
                    f"Narrative thread around {label} ({len(cluster_frags)} fragments)"
                ),
            )
            threads.append(thread)

        threads.sort(key=lambda t: t.fragment_count, reverse=True)

        logger.info(
            "Detected %d thread(s) among %d fragment(s) (min_fragments=%d)",
            len(threads),
            len(fragments),
            min_frags,
        )
        return threads
