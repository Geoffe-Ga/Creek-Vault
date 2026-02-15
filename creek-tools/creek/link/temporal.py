"""Temporal proximity linker stub.

Provides the ``TemporalLinker`` class which will find fragments created
within a configurable time window of each other.  This module is a stub —
the real implementation comes in issues #28-33.
"""

import logging

from creek.models import Fragment

logger = logging.getLogger(__name__)


class TemporalLinker:
    """Find temporal proximity links between fragments.

    This is a stub implementation that logs what it would do and returns
    an empty list.  The real temporal linking logic will be added in later
    issues.
    """

    def find_temporal_links(
        self, fragments: list[Fragment], window_hours: int
    ) -> list[tuple[str, str]]:
        """Find fragment pairs created within a time window of each other.

        This is a stub that returns an empty list.  The real implementation
        will compare creation timestamps and return pairs within the
        configured window.

        Args:
            fragments: List of fragments to check for temporal proximity.
            window_hours: Maximum hours between creation times to consider
                fragments temporally linked.

        Returns:
            A list of ``(fragment_id_a, fragment_id_b)`` tuples for each
            temporal link found.  Currently returns an empty list.
        """
        logger.info(
            "Stub: would find temporal links among %d fragment(s) "
            "within %d-hour window",
            len(fragments),
            window_hours,
        )
        return []
