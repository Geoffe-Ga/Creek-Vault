"""Review queue generation for fragments needing human review.

Generates a markdown file with checkboxes for fragments that require
human review — either because their classification confidence is low,
they are unclassified, or their source platform is configured for
mandatory human review.
"""

import logging
from pathlib import Path
from typing import Final

from creek.config import ClassificationConfig
from creek.models import Confidence, Fragment, Frequency, PrivacyTier
from creek.time import now_la

logger = logging.getLogger(__name__)

_REVIEW_QUEUE_RELPARTS: Final[tuple[str, ...]] = ("00-Creek-Meta", "Processing-Log")
"""Vault-relative directory the review queue is written to (#883).

Beside the compile-gap log and the lint reports — machine-written material
that belongs out of the operator's daily field of view. Path *components*
rather than a joined string so the join stays platform-correct.
"""

# Confidence levels considered "low" — fragments with these need review.
_LOW_CONFIDENCE_LEVELS: frozenset[str] = frozenset(
    {
        Confidence.MUSING,
        Confidence.EXPLORING,
    }
)


class ReviewQueueGenerator:
    """Generates a markdown review queue for uncertain fragments.

    Determines which fragments need human review based on classification
    confidence, source platform, and classification completeness, then
    writes a markdown file with checkboxes for each flagged fragment.

    Attributes:
        config: Classification pipeline configuration.
    """

    def __init__(self, config: ClassificationConfig | None = None) -> None:
        """Initialize the review queue generator.

        Args:
            config: Classification configuration. If None, uses defaults.
        """
        self.config = config or ClassificationConfig()

    def needs_review(self, fragment: Fragment) -> bool:
        """Check whether a fragment should be flagged for human review.

        A fragment needs review if any of the following are true:

        - Its primary frequency is UNCLASSIFIED
        - Its source platform is in the human_review_sources list
        - Its voice confidence is None or in the low-confidence set
        - Its privacy tier is INTIMATE (per ontology §13.2)

        Args:
            fragment: The fragment to evaluate.

        Returns:
            True if the fragment should be reviewed by a human.
        """
        if fragment.privacy_tier == PrivacyTier.INTIMATE:
            return True

        if fragment.frequency.primary == Frequency.UNCLASSIFIED:
            return True

        if fragment.source.platform in self.config.human_review_sources:
            return True

        if fragment.voice.confidence is None:
            return True

        return fragment.voice.confidence in _LOW_CONFIDENCE_LEVELS

    def generate_queue(
        self,
        fragments: list[Fragment],
        vault_path: Path,
    ) -> Path:
        """Write a review queue markdown file for fragments needing review.

        Filters the given fragments to those needing review, then writes
        a markdown file with checkboxes for each one, under
        :data:`_REVIEW_QUEUE_RELPARTS` beside Creek's other machine-written
        logs. It used to land in the vault root — the folder an operator
        opens in Obsidian every day — and was the only root-level
        bare-filename write in ``creek/`` or ``creek_mcp/`` (#883).

        The ``review-queue-<timestamp>.md`` name is unchanged, and that is an
        interface rather than a detail:
        :class:`creek.clean.hygiene.StaleReviewScanner` finds queues by
        ``rglob("review-queue-*.md")`` and ages them by ``strptime`` on the
        stem. Renaming while relocating would silently stop
        ``creek clean stale-reviews`` from ever expiring a queue again — and
        would look like it worked, because the scan would simply find nothing.
        ``rglob`` also still reaches queues left in the root by earlier
        versions, so nothing already written becomes invisible.

        Args:
            fragments: List of fragments to evaluate.
            vault_path: Path to the vault; the queue is written to the
                processing-log subdirectory beneath it, which is created if
                absent.

        Returns:
            Path to the generated review queue markdown file.
        """
        needs_review = [f for f in fragments if self.needs_review(f)]
        logger.info(
            "Review queue: %d of %d fragments need review",
            len(needs_review),
            len(fragments),
        )

        timestamp = now_la().strftime("%Y-%m-%d_%H%M%S")
        output_dir = vault_path.joinpath(*_REVIEW_QUEUE_RELPARTS)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"review-queue-{timestamp}.md"

        lines = self._build_markdown(needs_review)
        output_path.write_text("\n".join(lines) + "\n")

        logger.info("Review queue written to %s", output_path)
        return output_path

    def _build_markdown(self, fragments: list[Fragment]) -> list[str]:
        """Build markdown lines for the review queue.

        Args:
            fragments: Fragments that need review.

        Returns:
            List of markdown lines including header and checkboxes.
        """
        lines: list[str] = [
            "# Classification Review Queue",
            "",
            f"Generated: {now_la().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"Fragments to review: {len(fragments)}",
            "",
        ]

        if not fragments:
            lines.append("No fragments require review.")
            return lines

        lines.extend(("---", ""))

        for frag in fragments:
            lines.extend(self._format_fragment_entry(frag))
            lines.append("")

        return lines

    def _format_fragment_entry(self, fragment: Fragment) -> list[str]:
        """Format a single fragment as a review queue entry.

        Args:
            fragment: The fragment to format.

        Returns:
            List of markdown lines for this fragment's entry.
        """
        freq = fragment.frequency.primary
        phase = fragment.wavelength.phase
        source = fragment.source.platform
        return [
            f"- [ ] **{fragment.title}** (`{fragment.id}`)",
            f"  - Source: {source}",
            f"  - Frequency: {freq}",
            f"  - Phase: {phase}",
        ]
