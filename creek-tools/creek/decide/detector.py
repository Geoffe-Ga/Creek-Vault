"""Decision detection — scan fragments for decision-relevant content.

Identifies fragments that contain decision-related language such as
"should I", "trying to decide", "weighing options", etc.  Detected
fragments are candidates for auto-generating Decision notes in the
vault's ``08-Decisions/Active/`` directory.

Detection uses word-boundary regex matching with positional weighting
(title 3x, first paragraph 2x, body 1x), consistent with the
rule-based classification approach in :mod:`creek.classify.rules`.
"""

import logging
import re

from creek.models import Decision, DecisionStatus, Fragment

logger = logging.getLogger(__name__)

# ---- Decision Signal Keywords ----

DECISION_SIGNALS: list[str] = [
    "should I",
    "should i",
    "trying to decide",
    "weighing options",
    "not sure whether",
    "deciding between",
    "torn between",
    "on the fence",
    "debating whether",
    "considering whether",
    "might need to choose",
    "hard choice",
    "big decision",
    "life decision",
    "crossroads",
    "dilemma",
    "trade-off",
    "tradeoff",
    "pros and cons",
    "what if I",
    "what if i",
    "need to decide",
    "can't decide",
    "make up my mind",
    "which path",
    "either way",
]
"""Keywords and phrases that indicate decision-relevant content."""

# ---- Positional Weights ----

TITLE_WEIGHT: int = 3
"""Score multiplier for matches found in the title."""

FIRST_PARA_WEIGHT: int = 2
"""Score multiplier for matches found in the first paragraph."""

BODY_WEIGHT: int = 1
"""Score multiplier for matches found in the remaining body."""

DEFAULT_THRESHOLD: int = 2
"""Minimum score for a fragment to be flagged as decision-relevant."""


def _count_signal_matches(text: str, signals: list[str]) -> int:
    """Count word-boundary signal matches in text.

    Uses ``re.findall`` with word boundaries to prevent partial-word
    matches.

    Args:
        text: The text to search.
        signals: List of signal phrases to look for.

    Returns:
        Total number of signal matches found.
    """
    count = 0
    for signal in signals:
        escaped = re.escape(signal)
        count += len(re.findall(rf"\b{escaped}\b", text, re.IGNORECASE))
    return count


def _split_content_sections(content: str) -> tuple[str, str]:
    """Split content into first paragraph and body.

    The first paragraph is everything before the first blank line.
    The body is everything after.

    Args:
        content: Full content string.

    Returns:
        Tuple of (first_paragraph, body).
    """
    parts = content.split("\n\n", maxsplit=1)
    first_para = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    return first_para, body


class DecisionDetector:
    """Scan fragments for decision-relevant content.

    Scores fragments against decision signal keywords using positional
    weighting.  Fragments scoring above the configured threshold are
    flagged as decision-relevant and can be used to auto-generate
    Decision notes.

    Attributes:
        threshold: Minimum score required to flag a fragment.
    """

    def __init__(self, threshold: int = DEFAULT_THRESHOLD) -> None:
        """Initialise the detector with a score threshold.

        Args:
            threshold: Minimum score for a fragment to be flagged
                as decision-relevant.
        """
        self.threshold = threshold

    def score_fragment(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> int:
        """Score a fragment for decision-relevant keywords.

        Uses positional weighting: title matches count 3x, first
        paragraph 2x, and body 1x.

        Args:
            fragment: The fragment to score.
            content: The markdown body text to scan.

        Returns:
            The total decision relevance score.
        """
        title = fragment.title
        first_para, body = _split_content_sections(content)

        score = (
            _count_signal_matches(title, DECISION_SIGNALS) * TITLE_WEIGHT
            + _count_signal_matches(first_para, DECISION_SIGNALS) * FIRST_PARA_WEIGHT
            + _count_signal_matches(body, DECISION_SIGNALS) * BODY_WEIGHT
        )
        return score

    def is_decision_relevant(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> bool:
        """Check whether a fragment is decision-relevant.

        Args:
            fragment: The fragment to check.
            content: The markdown body text to scan.

        Returns:
            ``True`` if the fragment's score meets or exceeds the threshold.
        """
        return self.score_fragment(fragment, content) >= self.threshold

    def detect(
        self,
        fragments: list[Fragment],
        contents: list[str] | None = None,
    ) -> list[Fragment]:
        """Filter fragments to those containing decision-relevant content.

        Args:
            fragments: List of fragments to scan.
            contents: Optional parallel list of content strings.  If not
                provided, only fragment titles are scored.

        Returns:
            List of fragments that scored above the threshold.
        """
        if contents is None:
            contents = [""] * len(fragments)

        relevant: list[Fragment] = []
        for fragment, content in zip(fragments, contents, strict=True):
            if self.is_decision_relevant(fragment, content):
                relevant.append(fragment)

        logger.info(
            "Decision detection: %d of %d fragment(s) flagged",
            len(relevant),
            len(fragments),
        )
        return relevant

    def create_decision(
        self,
        fragment: Fragment,
    ) -> Decision:
        """Create a draft Decision from a decision-relevant fragment.

        The decision starts in the ``SENSING`` status and records the
        fragment's frequency context and wavelength phase.

        Args:
            fragment: The decision-relevant fragment.

        Returns:
            A new ``Decision`` in the sensing phase.
        """
        freq_context = []
        if fragment.frequency.primary != "unclassified":
            freq_context.append(fragment.frequency.primary)
        freq_context.extend(fragment.frequency.secondary)

        wavelength_phase = str(fragment.wavelength.phase)

        return Decision(
            title=f"Decision: {fragment.title}",
            status=DecisionStatus.SENSING,
            frequency_context=freq_context,
            wavelength_phase_at_opening=wavelength_phase,
            relevant_threads=list(fragment.threads),
            tags=list(fragment.tags),
        )
