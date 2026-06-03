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
# Good enough for a rate baseline. Public so the FEAT-040.7 rule-of-three
# tell locates exactly the triads the fingerprint counted.
TRIAD_RE = re.compile(
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
    return rate_per_kwords(len(TRIAD_RE.findall(text)), text)


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


# --- rhetorical features (FEAT-040.6) --------------------------------------
# Public compiled patterns, shared by the fingerprint measurers below and the
# rhetorical-detector locators, so each side counts/locates the same spans.

SIGNIFICANCE_RE = re.compile(
    r"\b(?:"
    r"pivotal moment|significant shift|enduring legacy|lasting legacy|"
    r"indelible mark|rich tapestry|stands as a testament|"
    r"contributing to the broader|represents a (?:significant|major|profound)|"
    r"marking a (?:pivotal|significant|key|crucial)|"
    r"plays? a (?:vital|crucial|key|pivotal|significant) role|"
    r"underscor\w+ (?:the|its) (?:importance|significance)|"
    r"broader (?:movement|context|history|narrative|trend)"
    r")\b",
    re.IGNORECASE,
)

SUPERFICIAL_ING_RE = re.compile(
    r",\s+(?:highlight|underscor|emphasiz|reflect|symboliz|showcas|"
    r"demonstrat|illustrat|reinforc|cement|contribut|solidif|foster|"
    r"enhanc|ensur)\w*ing\b"
    r"|\b(?:generat\w+|spark\w*|prompt\w+) (?:debate|discussion|reflection) about\b",
    re.IGNORECASE,
)

PEACOCK_RE = re.compile(
    r"\b(?:boasts|vibrant|nestled|breathtaking|renowned|bustling|stunning|"
    r"picturesque|charming|idyllic)\b"
    r"|\bin the heart of\b|\brich cultural heritage\b|\bnatural beauty\b",
    re.IGNORECASE,
)

VAGUE_ATTRIBUTION_RE = re.compile(
    r"\b(?:observers have (?:cited|noted|argued)|experts (?:argue|say|note|believe)|"
    r"critics (?:argue|say|note)|some (?:critics|scholars|observers|sources)|"
    r"several (?:sources|publications|scholars)|industry reports|"
    r"studies (?:show|suggest|indicate)|"
    r"it is widely (?:believed|held|regarded|considered)|"
    r"many (?:believe|argue|consider))\b",
    re.IGNORECASE,
)


def significance_phrase_density(text: str) -> float:
    """Return significance/legacy/broader-trend phrases per 1000 words."""
    return rate_per_kwords(len(SIGNIFICANCE_RE.findall(text)), text)


def superficial_ing_density(text: str) -> float:
    """Return trailing ``-ing`` analysis clauses per 1000 words."""
    return rate_per_kwords(len(SUPERFICIAL_ING_RE.findall(text)), text)


def peacock_density(text: str) -> float:
    """Return promotional / peacock terms per 1000 words."""
    return rate_per_kwords(len(PEACOCK_RE.findall(text)), text)


def vague_attribution_density(text: str) -> float:
    """Return vague-attribution / weasel phrases per 1000 words."""
    return rate_per_kwords(len(VAGUE_ATTRIBUTION_RE.findall(text)), text)


# --- discourse / structure features (FEAT-040.7) ---------------------------
# Public compiled patterns, shared by the fingerprint measurers below and the
# discourse-detector locators, so each side counts/locates the same spans.
# Each pattern matches the offending phrase as group 0 (no lead-in capture)
# unless noted, so a plain ``finditer`` span is the thing to surface.

# Antithesis / negative-parallelism. The alternations, in order:
#   1. ``not only X but [also] Y``.
#   2. Bare ``not <prep> X[,] but <prep> Y`` — antithesis built from parallel
#      prepositional phrases, the dominant AI tell from issue #516, e.g.
#      "Not through bliss, necessarily, but through outrage". The shared
#      preposition is what makes it *parallel* antithesis rather than a casual
#      concessive ("Not my best work but it is honest"), which must not flag.
#   3. ``it's not X, it's Y`` (contracted, comma-then-``it's``) and its
#      uncontracted twin ``it is not X[,] it is Y`` / ``it is not about X[,]
#      it is about Y``.
#   4. Cross-sentence subject restatement ``X isn't/weren't Y. <subject>
#      is/are/was/were/makes Z`` — the negated claim then its affirming echo,
#      e.g. "The parables weren't instruction manuals. They were field reports."
#      The echoed subject must be a *genuine* restatement: either a pronoun
#      (they/it/he/she/we) or the SAME noun repeated (``the <noun>`` …
#      ``the <noun>`` via the ``xnoun`` backreference). A fresh ``The <word>``
#      with a different noun is not antithesis and must not match, e.g.
#      "The algorithm doesn't converge. The teacher is patient."
#   5. Em-dash antithesis ``it doesn't … — it …``.
#   6. ``no X, no Y, just/but``.
# Calibration: every alternation requires a contrasting second clause (a bare
# negation never matches), so a single deliberate antithesis registers as one
# count and is gated by the surface margin, not a hard failure.
NEGATIVE_PARALLELISM_RE = re.compile(
    r"\bnot only\b[^.!?\n]{1,80}?\bbut(?: also)?\b"
    r"|\bnot (?P<prep>through|by|with|for|about|in|of|from|because)\b"
    r"[^.!?\n]{1,80}?,?\s*\bbut (?P=prep)\b"
    r"|\bit'?s not (?:just |merely |only )?[^.!?\n]{1,60}?,\s*it'?s\b"
    r"|\bit is not (?:just |merely |only |about )?[^.!?\n]{1,60}?,?\s*it is\b"
    r"|\b(?:the (?P<xnoun>[a-z]+) )?"
    r"(?:is|are|was|were|do|does)n'?t\b[^.!?\n]{1,80}?[.!?]\s+"
    r"(?:(?:they|it|he|she|we)|the (?P=xnoun))\b[^.!?\n]{0,12}?"
    r"\b(?:is|are|was|were|do|does|make|makes)\b"
    r"|\b(?:is|are|was|were|do|does)n'?t\b[^.!?\n]{1,60}?—"
    r"\s*it\b[^.!?\n]{0,40}?\b(?:is|makes?|does)\b"
    r"|\bno [a-z]+, no [a-z]+,?\s+(?:just|but)\b",
    re.IGNORECASE,
)

CHALLENGES_SECTION_RE = re.compile(
    r"\bdespite its\b[^.!?\n]{0,80}?\bfaces\b[^.!?\n]{0,40}?\bchallenges\b",
    re.IGNORECASE,
)

# Group 1 is the phrase; the line-start anchor is not part of the surfaced span.
LIST_TITLE_LEAD_RE = re.compile(
    r"^(?P<phrase>(?:the )?list of [^.\n]{1,80}? is (?:a|an)\b)",
    re.IGNORECASE | re.MULTILINE,
)

KNOWLEDGE_CUTOFF_RE = re.compile(
    r"\bas of my (?:last )?(?:knowledge update|knowledge cutoff|update)\b"
    r"|\bwhile specific details are limited in the available sources\b"
    r"|\blikely supports\b"
    r"|\bmaintains a low profile\b",
    re.IGNORECASE,
)

# First-person sourcing / provenance announcements. The model narrates that
# its prose was retrieved from the owner's journals/notes rather than weaving
# the material in as the owner's own live thinking (issue #517). Generic-prior
# tell: these should essentially never appear in finished prose. Kept
# conservative — match self-referential SOURCING announcements, not ordinary
# uses of "wrote"/"note" ("I wrote her a letter.", "Note the difference.").
#   1. ``my journal(s)/notes/entries`` and the verb ``journaling`` (the
#      ``my …`` branch also covers ``in my notes`` via the substring).
#   2. ``I('ve| have)? been journaling`` / ``… been writing in my
#      journals/notes/diary`` — narrating the act of keeping a journal as the
#      source. Bare "been writing" is dropped: it matches ordinary prose
#      ("I have been writing code.") without announcing a source (#525).
#   3. ``from one of (them|my journals/notes/entries)`` — quoting a source.
#   4. ``as I wrote (in …)`` / ``one of my (journals|notes|entries)`` — pointing
#      at a prior written-down source.
#   5. ``my journals keep …`` — personifying the corpus as a source.
PROVENANCE_RE = re.compile(
    r"\bmy (?:journals?|notes|entries)\b"
    r"|\bjournaling\b"
    r"|\bi(?:'ve| have)? been journaling\b"
    r"|\bi(?:'ve| have)? been writing in my (?:journals?|notes?|diary)\b"
    r"|\bfrom one of (?:them|my (?:journals?|notes|entries))\b"
    r"|\bas i wrote\b"
    r"|\bone of my (?:journals?|notes|entries)\b",
    re.IGNORECASE,
)

DIDACTIC_DISCLAIMER_RE = re.compile(
    r"\bit'?s important to note\b"
    r"|\bimportant to note that\b"
    r"|\b(?:it'?s|it is) worth noting\b"
    r"|\bworth noting that\b"
    r"|\bit should be noted\b"
    r"|\bmay vary\b",
    re.IGNORECASE,
)

# Group 1 is the phrase; a preceding sentence terminator / line start anchors
# it to a section boundary but is not part of the surfaced span.
SECTION_SUMMARY_RE = re.compile(
    r"(?:^|[.!?]\s+)(?P<phrase>(?:in summary|in conclusion|overall|"
    r"to summarize|to sum up|all in all)),",
    re.IGNORECASE | re.MULTILINE,
)

COMM_BOILERPLATE_RE = re.compile(
    r"\bi hope this helps\b"
    r"|\bwould you like (?:me )?to\b"
    r"|\bcertainly!"
    r"|\blet me know if\b"
    r"|\bfeel free to\b"
    r"|\barticles for creation\b"
    r"|\bsubmitting (?:this|the) (?:draft|article)\b"
    r"|^subject:",
    re.IGNORECASE | re.MULTILINE,
)


def negative_parallelism_density(text: str) -> float:
    """Return negative-parallelism constructions per 1000 words."""
    return rate_per_kwords(len(NEGATIVE_PARALLELISM_RE.findall(text)), text)


def challenges_section_density(text: str) -> float:
    """Return ``Despite its X, Y faces challenges`` formulas per 1000 words."""
    return rate_per_kwords(len(CHALLENGES_SECTION_RE.findall(text)), text)


def list_title_lead_density(text: str) -> float:
    """Return list/non-proper-noun-title-as-entity leads per 1000 words."""
    return rate_per_kwords(len(LIST_TITLE_LEAD_RE.findall(text)), text)


def knowledge_cutoff_density(text: str) -> float:
    """Return knowledge-cutoff / not-documented disclaimers per 1000 words."""
    return rate_per_kwords(len(KNOWLEDGE_CUTOFF_RE.findall(text)), text)


def provenance_density(text: str) -> float:
    """Return first-person sourcing/provenance announcements per 1000 words."""
    return rate_per_kwords(len(PROVENANCE_RE.findall(text)), text)


def didactic_disclaimer_density(text: str) -> float:
    """Return didactic disclaimers (important to note, may vary) per 1000 words."""
    return rate_per_kwords(len(DIDACTIC_DISCLAIMER_RE.findall(text)), text)


def section_summary_density(text: str) -> float:
    """Return sentence-initial section summaries per 1000 words."""
    return rate_per_kwords(len(SECTION_SUMMARY_RE.findall(text)), text)


def comm_boilerplate_density(text: str) -> float:
    """Return collaborative-comm boilerplate phrases per 1000 words."""
    return rate_per_kwords(len(COMM_BOILERPLATE_RE.findall(text)), text)


FINGERPRINT_FEATURES: dict[str, Extractor] = {
    "em_dash_density": em_dash_density,
    "curly_quote_density": curly_quote_density,
    "ai_vocab_density": ai_vocab_density,
    "marketing_verb_ratio": marketing_verb_ratio,
    "sentence_length_mean": sentence_length_mean,
    "transition_opener_rate": transition_opener_rate,
    "rule_of_three_rate": rule_of_three_rate,
    "concrete_density": concrete_density,
    "significance_phrase_density": significance_phrase_density,
    "superficial_ing_density": superficial_ing_density,
    "peacock_density": peacock_density,
    "vague_attribution_density": vague_attribution_density,
    "negative_parallelism_density": negative_parallelism_density,
    "challenges_section_density": challenges_section_density,
    "list_title_lead_density": list_title_lead_density,
    "provenance_density": provenance_density,
    "didactic_disclaimer_density": didactic_disclaimer_density,
    "section_summary_density": section_summary_density,
    "comm_boilerplate_density": comm_boilerplate_density,
}
"""The feature_key -> extractor map the fingerprint measures over the
user's corpus. Detector tells (FEAT-040.3 through .7) query these same
keys, so their ``measure`` functions must reuse the matching extractor
here."""
