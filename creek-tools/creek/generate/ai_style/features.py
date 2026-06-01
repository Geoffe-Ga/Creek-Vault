"""Feature extractors: the single source of truth for measuring writing.

Each extractor maps a body of text to a scalar rate (per 1000 words unless
noted). The voice fingerprint (FEAT-040.2) measures the user's baseline
with these, and later detector tells (FEAT-040.3 through .7) reuse the
*same* functions as their ``measure``, so a draft's rate and the user's
rate are always computed on the same scale and the voice-distance is
apples-to-apples by construction.

All extractors are pure and deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from creek.generate.ai_style.tells import rate_per_kwords, word_count

Extractor = Callable[[str], float]
"""Signature shared by every fingerprint feature extractor."""

# --- patterns --------------------------------------------------------------

_EM_DASH_RE = re.compile("\u2014")  # em dash
_CURLY_RE = re.compile("[\u2018\u2019\u201c\u201d]")  # curly quotes / apostrophes
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")
_WORD_RE = re.compile(r"\b\w+\b")

# NOTE: substring matching means "features" also hits the noun ("three
# killer features"), which can inflate the ratio in personal writing. Both
# the fingerprint and the lexical detector share this list and tolerate the
# noise: it is vault-relative, so a noun-heavy writer's own baseline rises
# with them. Verb-context narrowing is possible future work, not yet done.
MARKETING_VERBS = (
    "serves as",
    "stands as",
    "boasts",
    "features",
    "offers",
    "maintains",
)
# Word-bounded marketing-verb matcher, shared by the measurer below and the
# lexical locator, so both count/locate the same occurrences (e.g. neither
# matches "features" inside "misfeatures").
MARKETING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(v) for v in MARKETING_VERBS) + r")\b",
    re.IGNORECASE,
)
_COPULAS = (" is ", " are ", " was ", " were ", " has ", " have ")

TRANSITION_OPENERS = (
    "additionally",
    "moreover",
    "furthermore",
    "notably",
    "consequently",
)

# A small, era-spanning sample of the AI-vocabulary list, shared with the
# lexical detector. It is intentionally a representative sample (not an
# exhaustive catalog); the fingerprint only needs a consistent measure of
# how often the user reaches for these words.
AI_VOCAB = (
    "delve",
    "tapestry",
    "underscore",
    "pivotal",
    "testament",
    "vibrant",
    "intricate",
    "showcasing",
    "leverage",
    "robust",
)

CONCRETE_RE = re.compile(r"\bconcrete\b", re.IGNORECASE)

# Baseline heuristic: matches single-word triads ("red, white, and blue")
# but not multi-word items ("a vivid red, a soft white, and a deep blue").
# Good enough for a rate baseline; FEAT-040.7 can broaden it if needed.
_TRIAD_RE = re.compile(
    r"\b\w+,\s+\w+,?\s+(?:and|or)\s+\w+\b",
)


def _count_phrases(text: str, phrases: tuple[str, ...]) -> int:
    """Return the total case-insensitive occurrences of *phrases* in *text*.

    Args:
        text: The body to scan.
        phrases: Substrings to count.

    Returns:
        Summed occurrence count across all phrases.
    """
    lowered = text.lower()
    return sum(lowered.count(phrase) for phrase in phrases)


def em_dash_density(text: str) -> float:
    """Return em-dashes per 1000 words."""
    return rate_per_kwords(len(_EM_DASH_RE.findall(text)), text)


def curly_quote_density(text: str) -> float:
    """Return curly quotation marks / apostrophes per 1000 words."""
    return rate_per_kwords(len(_CURLY_RE.findall(text)), text)


def ai_vocab_density(text: str) -> float:
    """Return AI-vocabulary word occurrences per 1000 words."""
    lowered = text.lower()
    hits = sum(len(re.findall(rf"\b{re.escape(w)}\b", lowered)) for w in AI_VOCAB)
    return rate_per_kwords(hits, text)


def marketing_verb_ratio(text: str) -> float:
    """Return marketing verbs as a fraction of marketing-verbs-plus-copulas.

    ``serves as`` / ``boasts`` / ``features`` … relative to plain ``is`` /
    ``are`` / ``has``. ``0.0`` when neither appears. This is a ratio, not a
    per-1000-words rate, so it is comparable across document lengths.

    Args:
        text: The body to scan.

    Returns:
        ``marketing / (marketing + copula)`` in ``[0, 1]``; ``0.0`` when the
        denominator is zero.
    """
    marketing = len(MARKETING_RE.findall(text))
    copula = _count_phrases(text, _COPULAS)
    denom = marketing + copula
    return marketing / denom if denom else 0.0


def sentence_length_mean(text: str) -> float:
    """Return the mean sentence length in words.

    Args:
        text: The body to scan.

    Returns:
        Mean words per sentence; the whole-text word count when no
        sentence terminator is present (a single implied sentence).
    """
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return float(word_count(text))
    lengths = [len(_WORD_RE.findall(s)) for s in sentences]
    return sum(lengths) / len(lengths)


def transition_opener_rate(text: str) -> float:
    """Return sentence-initial transition words per 1000 words.

    Counts sentences whose first word is a known transition opener
    (``Additionally``, ``Moreover``, …).

    Args:
        text: The body to scan.

    Returns:
        Transition-opener count per 1000 words.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    hits = 0
    for sentence in sentences:
        first = _WORD_RE.findall(sentence)
        if first and first[0].lower() in TRANSITION_OPENERS:
            hits += 1
    return rate_per_kwords(hits, text)


def rule_of_three_rate(text: str) -> float:
    """Return ``a, b, and c`` triads per 1000 words (a padding heuristic)."""
    return rate_per_kwords(len(_TRIAD_RE.findall(text)), text)


def concrete_density(text: str) -> float:
    """Return occurrences of the word "concrete" per 1000 words.

    Fingerprinted so the "concrete" tell (a comment-context AI tell) is
    vault-relative: a writer who routinely says "concrete evidence" or
    "concrete slab" has a high baseline and is not flagged for it.

    Args:
        text: The body to scan.

    Returns:
        Occurrences of "concrete" per 1000 words.
    """
    return rate_per_kwords(len(CONCRETE_RE.findall(text)), text)


FINGERPRINT_FEATURES: dict[str, Extractor] = {
    "em_dash_density": em_dash_density,
    "curly_quote_density": curly_quote_density,
    "ai_vocab_density": ai_vocab_density,
    "marketing_verb_ratio": marketing_verb_ratio,
    "sentence_length_mean": sentence_length_mean,
    "transition_opener_rate": transition_opener_rate,
    "rule_of_three_rate": rule_of_three_rate,
    "concrete_density": concrete_density,
}
"""The feature_key -> extractor map the fingerprint measures over the
user's corpus. Detector tells (FEAT-040.3 through .7) query these same
keys, so their ``measure`` functions must reuse the matching extractor
here."""
