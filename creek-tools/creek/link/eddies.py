"""Eddy detection stub.

Provides the ``EddyDetector`` class which will identify topic clusters
(eddies) where multiple threads converge.  This module is a stub — the
real implementation comes in issues #28-33.
"""

import logging

from creek.models import Eddy, Fragment

logger = logging.getLogger(__name__)


class EddyDetector:
    """Detect topic cluster eddies across a collection of fragments.

    This is a stub implementation that logs what it would do and returns
    an empty list.  The real eddy detection logic will be added in later
    issues.
    """

    def detect_eddies(self, fragments: list[Fragment]) -> list[Eddy]:
        """Detect topic cluster eddies in a set of fragments.

        This is a stub that returns an empty list.  The real implementation
        will analyse fragment relationships to identify convergence points.

        Args:
            fragments: List of fragments to scan for eddy patterns.

        Returns:
            A list of detected ``Eddy`` objects.  Currently returns an
            empty list.
        """
        logger.info(
            "Stub: would detect eddies among %d fragment(s)",
            len(fragments),
        )
        return []
