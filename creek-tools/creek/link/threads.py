"""Thread detection stub.

Provides the ``ThreadDetector`` class which will identify recurring
narrative threads across fragments.  This module is a stub — the real
implementation comes in issues #28-33.
"""

import logging

from creek.models import Fragment, Thread

logger = logging.getLogger(__name__)


class ThreadDetector:
    """Detect narrative threads across a collection of fragments.

    This is a stub implementation that logs what it would do and returns
    an empty list.  The real thread detection logic will be added in later
    issues.
    """

    def detect_threads(self, fragments: list[Fragment]) -> list[Thread]:
        """Detect recurring narrative threads in a set of fragments.

        This is a stub that returns an empty list.  The real implementation
        will analyse fragment content and metadata to identify threads.

        Args:
            fragments: List of fragments to scan for thread patterns.

        Returns:
            A list of detected ``Thread`` objects.  Currently returns an
            empty list.
        """
        logger.info(
            "Stub: would detect threads among %d fragment(s)",
            len(fragments),
        )
        return []
