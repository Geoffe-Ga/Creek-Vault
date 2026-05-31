"""Lexical detectors (FEAT-040.5): vault-relative AI-vocabulary and friends.

These tells *measure* lexical features using the same extractors the
fingerprint uses (:mod:`creek.generate.ai_style.features`), so the scanner
compares a draft's rate to the user's stored rate for the same key. A word
or construction is only flagged when the draft over-uses it *relative to
the user* — a user who genuinely writes "tapestry" or "serves as" is never
flagged for it. Detection only; no rewriting.

Importing this module registers its tells in the global registry.
"""

from __future__ import annotations

import re

from creek.generate.ai_style import features
from creek.generate.ai_style.model import Span
from creek.generate.ai_style.tells import Tell, register


def _word_alt(words: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a case-insensitive, word-bounded alternation of *words*."""
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)


_AI_VOCAB_RE = _word_alt(features.AI_VOCAB)
_MARKETING_RE = _word_alt(features.MARKETING_VERBS)
# Sentence-initial transition word (start of text or after a terminator).
# Uses ``[.!?]+`` to match transition_opener_rate's sentence splitter, so the
# locator finds exactly what the measurer counts (e.g. after "Done!!").
_TRANSITION_RE = re.compile(
    r"(?:^|[.!?]+\s+)(" + "|".join(features.TRANSITION_OPENERS) + r")\b",
    re.IGNORECASE | re.MULTILINE,
)


def _spans(pattern: re.Pattern[str], text: str, *, group: int = 0) -> list[Span]:
    """Return the spans of *pattern*'s *group* across *text*."""
    return [Span(m.start(group), m.end(group)) for m in pattern.finditer(text)]


def _locate_ai_vocab(text: str) -> list[Span]:
    """Locate AI-vocabulary word occurrences."""
    return _spans(_AI_VOCAB_RE, text)


def _locate_marketing(text: str) -> list[Span]:
    """Locate marketing-verb phrase occurrences."""
    return _spans(_MARKETING_RE, text)


def _locate_transitions(text: str) -> list[Span]:
    """Locate sentence-initial transition openers (the word itself)."""
    return _spans(_TRANSITION_RE, text, group=1)


def _locate_concrete(text: str) -> list[Span]:
    """Locate occurrences of the word "concrete"."""
    return _spans(features.CONCRETE_RE, text)


AI_VOCABULARY = register(
    Tell(
        id="ai_vocabulary",
        category="lexical",
        feature_key="ai_vocab_density",
        handling="prevent",
        polarity="avoid",
        description="AI-vocabulary words used more than you do (delve, tapestry, ...).",
        caveat="Only flagged above your own measured rate; a word you genuinely "
        "use is your voice, not a tell.",
        measure=features.ai_vocab_density,
        locate=_locate_ai_vocab,
        # Density, not single hits: require >~2 over-baseline AI words per
        # 1000 words so one coincidental "robust" never flags (the guide:
        # "one or two of these words may be coincidental").
        margin=2.0,
    ),
)

COPULATIVE_AVOIDANCE = register(
    Tell(
        id="copulative_avoidance",
        category="lexical",
        feature_key="marketing_verb_ratio",
        handling="prevent",
        polarity="avoid",
        description="Marketing verbs (serves as, boasts, features) over plain is/has.",
        caveat="A ratio relative to your own copula use; some writers genuinely "
        "favour 'serves as'.",
        measure=features.marketing_verb_ratio,
        locate=_locate_marketing,
        # Ratio feature in [0, 1]; a smaller margin than the per-1000-words default.
        margin=0.15,
    ),
)

TRANSITION_OPENER = register(
    Tell(
        id="transition_opener",
        category="lexical",
        feature_key="transition_opener_rate",
        handling="prevent",
        polarity="avoid",
        description="Sentences opening with Additionally/Moreover/Furthermore/...",
        caveat="A few transitions are normal; only an elevated rate above yours "
        "is flagged.",
        measure=features.transition_opener_rate,
        locate=_locate_transitions,
        # A single sentence-initial transition is normal prose; require an
        # elevated rate above the user's before flagging.
        margin=1.5,
    ),
)

CONCRETE_COMMENT = register(
    Tell(
        id="concrete_comment",
        category="lexical",
        feature_key="concrete_rate",
        handling="surface",
        polarity="avoid",
        description='Overuse of "concrete" (concrete evidence/examples) in comments.',
        caveat="Comment context only; the material/literal senses of 'concrete' "
        "are not the target.",
        measure=features.concrete_density,
        locate=_locate_concrete,
        contexts=frozenset({"comment"}),
        # Default per-1000-words margin: one stray "concrete" over the user's
        # baseline is tolerated; a cluster ("concrete evidence ... concrete
        # examples") in a comment is the tell.
        margin=2.0,
    ),
)
