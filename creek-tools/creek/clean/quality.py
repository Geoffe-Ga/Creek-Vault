"""Content quality scorer — length, entropy, stop-word ratio metrics.

Evaluates raw text content against configurable quality heuristics and
returns a structured :class:`QualityResult` with a 0.0--1.0 score,
an action recommendation (``accept``, ``review``, or ``skip``), and
human-readable reasons.

Scoring metrics:
- **Content length** (character and word count)
- **Shannon entropy** (low entropy = repetitive noise)
- **Stop-word ratio** (high ratio = filler content)
- **URL-only detection** (content exclusively URLs)
- **Emoji-only detection** (content exclusively emoji)
- **Alphanumeric presence** (must contain ``[a-zA-Z0-9]``)
"""

import math
import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Stop-word list (common English function words)
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "their",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "not",
        "no",
        "so",
        "if",
        "as",
        "up",
    }
)
"""Common English stop words for ratio calculation."""

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_URL_PATTERN: re.Pattern[str] = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)
"""Matches HTTP/HTTPS URLs."""

_EMOJI_PATTERN: re.Pattern[str] = re.compile(
    r"[\U0001f600-\U0001f64f"
    r"\U0001f300-\U0001f5ff"
    r"\U0001f680-\U0001f6ff"
    r"\U0001f1e0-\U0001f1ff"
    r"\U00002702-\U000027b0"
    r"\U0000fe00-\U0000fe0f"
    r"\U0001f900-\U0001f9ff"
    r"\U0001fa00-\U0001fa6f"
    r"\U0001fa70-\U0001faff"
    r"\U00002600-\U000026ff]+",
)
"""Matches common emoji character ranges."""

_ALPHANUMERIC_PATTERN: re.Pattern[str] = re.compile(r"[a-zA-Z0-9]")
"""Matches ASCII alphanumeric characters."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class QualityResult(BaseModel):
    """Structured result from quality scoring.

    Attributes:
        score: Quality score in the range [0.0, 1.0].
        action: Recommended action: ``accept``, ``review``, or ``skip``.
        reasons: Human-readable explanations of the score.
    """

    score: float = Field(ge=0.0, le=1.0)
    action: Literal["accept", "review", "skip"]
    reasons: list[str]


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


class QualityScorer:
    """Score content quality using length, entropy, and ratio heuristics.

    All thresholds are configurable through constructor parameters so
    callers can tune behaviour without depending on a config module.

    Attributes:
        min_length: Minimum character count for full score.
        min_words: Minimum word count for full score.
        entropy_threshold: Minimum Shannon entropy for full score.
        stop_word_threshold: Maximum stop-word ratio before penalty.
        accept_threshold: Minimum score to recommend ``accept``.
        review_threshold: Minimum score to recommend ``review``
            (below this is ``skip``).
    """

    def __init__(
        self,
        *,
        min_length: int = 50,
        min_words: int = 10,
        entropy_threshold: float = 3.0,
        stop_word_threshold: float = 0.7,
        accept_threshold: float = 0.6,
        review_threshold: float = 0.3,
    ) -> None:
        """Initialise the scorer with configurable thresholds.

        Args:
            min_length: Minimum character count for full length score.
            min_words: Minimum word count for full length score.
            entropy_threshold: Minimum Shannon entropy (bits) for full
                entropy score.
            stop_word_threshold: Maximum stop-word ratio (0.0--1.0)
                before applying a penalty.
            accept_threshold: Score at or above this is ``accept``.
            review_threshold: Score at or above this (but below
                ``accept_threshold``) is ``review``; below is ``skip``.
        """
        self.min_length = min_length
        self.min_words = min_words
        self.entropy_threshold = entropy_threshold
        self.stop_word_threshold = stop_word_threshold
        self.accept_threshold = accept_threshold
        self.review_threshold = review_threshold

    def score(self, content: str) -> QualityResult:
        """Evaluate content and return a quality result.

        Applies all quality checks in sequence, accumulates partial
        scores, and maps the aggregate to an action recommendation.

        Args:
            content: Raw text content to evaluate.

        Returns:
            A :class:`QualityResult` with score, action, and reasons.
        """
        reasons: list[str] = []

        stripped = content.strip()
        if not stripped:
            return QualityResult(
                score=0.0,
                action="skip",
                reasons=["Content is empty or whitespace-only"],
            )

        # Run individual checks (order matters: emoji and URL before
        # alphanumeric since emoji-only content lacks alphanumeric chars)
        checks = [
            self._check_emoji_only(stripped),
            self._check_url_only(stripped),
            self._check_alphanumeric(stripped),
        ]

        for check_result in checks:
            if check_result is not None:
                return check_result

        # Compute component scores
        length_score, length_reasons = self._score_length(stripped)
        entropy_score, entropy_reasons = self._score_entropy(stripped)
        stop_word_score, stop_reasons = self._score_stop_words(stripped)

        reasons.extend(length_reasons)
        reasons.extend(entropy_reasons)
        reasons.extend(stop_reasons)

        aggregate = self._aggregate(length_score, entropy_score, stop_word_score)

        action = self._determine_action(aggregate)

        return QualityResult(
            score=round(aggregate, 4),
            action=action,
            reasons=reasons,
        )

    # ---- Quick-reject checks ----

    def _check_alphanumeric(self, content: str) -> QualityResult | None:
        """Return a skip result if no alphanumeric characters are present.

        Args:
            content: Stripped text content.

        Returns:
            A skip :class:`QualityResult`, or ``None`` if alphanumeric
            characters are found.
        """
        if not _ALPHANUMERIC_PATTERN.search(content):
            return QualityResult(
                score=0.0,
                action="skip",
                reasons=["No alphanumeric characters found"],
            )
        return None

    def _check_url_only(self, content: str) -> QualityResult | None:
        """Return a skip result if content is exclusively URLs.

        Removes all URLs from the text and checks whether any
        meaningful non-whitespace content remains.

        Args:
            content: Stripped text content.

        Returns:
            A skip :class:`QualityResult`, or ``None`` if non-URL
            content is present.
        """
        without_urls = _URL_PATTERN.sub("", content).strip()
        if not without_urls:
            return QualityResult(
                score=0.0,
                action="skip",
                reasons=["Content consists exclusively of URLs"],
            )
        return None

    def _check_emoji_only(self, content: str) -> QualityResult | None:
        """Return a skip result if content is exclusively emoji.

        Removes all emoji characters and whitespace, then checks
        whether any other content remains.

        Args:
            content: Stripped text content.

        Returns:
            A skip :class:`QualityResult`, or ``None`` if non-emoji
            content is present.
        """
        without_emoji = _EMOJI_PATTERN.sub("", content).strip()
        if not without_emoji:
            return QualityResult(
                score=0.0,
                action="skip",
                reasons=["Content consists exclusively of emoji"],
            )
        return None

    # ---- Component scorers ----

    def _score_length(self, content: str) -> tuple[float, list[str]]:
        """Score based on character and word counts.

        Returns a score in [0.0, 1.0] and any penalty reasons.

        Args:
            content: Stripped text content.

        Returns:
            Tuple of (score, reasons).
        """
        reasons: list[str] = []
        char_count = len(content)
        word_count = len(content.split())

        char_score = min(char_count / max(self.min_length, 1), 1.0)
        word_score = min(word_count / max(self.min_words, 1), 1.0)
        combined = (char_score + word_score) / 2.0

        if char_count < self.min_length:
            reasons.append(f"Short content: {char_count} chars (min {self.min_length})")
        if word_count < self.min_words:
            reasons.append(f"Few words: {word_count} words (min {self.min_words})")

        return combined, reasons

    def _score_entropy(self, content: str) -> tuple[float, list[str]]:
        """Score based on Shannon entropy of character and word distributions.

        Computes both character-level and word-level entropy, then uses
        the minimum.  This catches both single-character repetition
        (low character entropy) and repeated-word patterns (low word
        entropy).  The ratio is squared to penalise low entropy more
        aggressively.

        Args:
            content: Stripped text content.

        Returns:
            Tuple of (score, reasons).
        """
        reasons: list[str] = []
        char_entropy = _shannon_entropy(content)
        word_entropy = _word_entropy(content)
        entropy = min(char_entropy, word_entropy)

        ratio = min(entropy / max(self.entropy_threshold, 0.01), 1.0)
        # Square the ratio so moderate shortfalls produce steep penalties
        score = ratio * ratio

        if entropy < self.entropy_threshold:
            reasons.append(
                f"Low entropy: {entropy:.2f} bits "
                f"(threshold {self.entropy_threshold:.2f})"
            )

        return score, reasons

    def _score_stop_words(self, content: str) -> tuple[float, list[str]]:
        """Score based on the ratio of stop words to total words.

        A high stop-word ratio suggests filler content with little
        substantive information.

        Args:
            content: Stripped text content.

        Returns:
            Tuple of (score, reasons).
        """
        reasons: list[str] = []
        words = content.lower().split()
        if not words:
            return 1.0, reasons

        stop_count = sum(1 for w in words if w in _STOP_WORDS)
        ratio = stop_count / len(words)

        if ratio > self.stop_word_threshold:
            penalty = (ratio - self.stop_word_threshold) / (
                1.0 - self.stop_word_threshold
            )
            score = max(1.0 - penalty, 0.0)
            reasons.append(
                f"High stop-word ratio: {ratio:.2%} "
                f"(threshold {self.stop_word_threshold:.0%})"
            )
        else:
            score = 1.0

        return score, reasons

    # ---- Aggregation and action mapping ----

    def _aggregate(
        self,
        length_score: float,
        entropy_score: float,
        stop_word_score: float,
    ) -> float:
        """Combine component scores into a single aggregate.

        Uses a weighted average (length 25 %, entropy 40 %, stop-words
        35 %) capped by the minimum component score.  This ensures that
        a single very-low metric drags the aggregate down even when
        other metrics are perfect.

        Args:
            length_score: Score from length check.
            entropy_score: Score from entropy check.
            stop_word_score: Score from stop-word check.

        Returns:
            Weighted aggregate in [0.0, 1.0].
        """
        weighted = 0.25 * length_score + 0.40 * entropy_score + 0.35 * stop_word_score
        # Cap at 1.5x the minimum component so one bad metric dominates
        floor = min(length_score, entropy_score, stop_word_score)
        return min(weighted, floor * 1.5)

    def _determine_action(
        self,
        score: float,
    ) -> Literal["accept", "review", "skip"]:
        """Map a numeric score to an action recommendation.

        Args:
            score: Aggregate quality score.

        Returns:
            One of ``accept``, ``review``, or ``skip``.
        """
        if score >= self.accept_threshold:
            return "accept"
        if score >= self.review_threshold:
            return "review"
        return "skip"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _word_entropy(text: str) -> float:
    """Compute Shannon entropy over whitespace-delimited word tokens.

    This complements character-level entropy by detecting repeated-word
    patterns that character entropy may miss (e.g. ``"hello "`` repeated
    100 times has moderate character entropy but zero word entropy).

    Args:
        text: Input text.

    Returns:
        Shannon entropy in bits over word tokens.  Returns 0.0 for
        empty or single-word input.
    """
    words = text.lower().split()
    if len(words) <= 1:
        return 0.0

    length = len(words)
    counts = Counter(words)
    entropy = 0.0
    for count in counts.values():
        prob = count / length
        if prob > 0:
            entropy -= prob * math.log2(prob)
    return entropy


def _shannon_entropy(text: str) -> float:
    """Compute Shannon entropy of a character distribution.

    Entropy is measured in bits (log base 2).  Higher values indicate
    more varied, information-rich content.

    Args:
        text: Input text.

    Returns:
        Shannon entropy in bits.  Returns 0.0 for empty input.
    """
    if not text:
        return 0.0

    length = len(text)
    counts = Counter(text)
    entropy = 0.0
    for count in counts.values():
        prob = count / length
        if prob > 0:
            entropy -= prob * math.log2(prob)
    return entropy
