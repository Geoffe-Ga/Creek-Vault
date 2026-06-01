"""Rhetorical detectors (FEAT-040.6): vault-relative substance tells.

These are the *substantive* AI tells — significance/legacy puffery,
superficial trailing ``-ing`` analysis, promotional peacockery, and vague
attribution. They point at deeper problems (ungrounded synthesis, padding,
opinion-as-fact), so they are **surface-only**: the scanner flags them for
review/rewrite, never auto-strips them. Like every FEAT-040 tell they are
vault-relative — a writer whose own prose runs grand or promotional has a
high baseline and is not flagged for it.

Each tell reuses the shared pattern + extractor from
:mod:`creek.generate.ai_style.features`, so the locator highlights exactly
what the measurer counted. Importing this module registers its tells.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.generate.ai_style import features
from creek.generate.ai_style.model import Span
from creek.generate.ai_style.tells import Tell, register

if TYPE_CHECKING:
    import re


def _spans(pattern: re.Pattern[str], text: str) -> list[Span]:
    """Return the spans of every *pattern* match in *text*."""
    return [Span(m.start(), m.end()) for m in pattern.finditer(text)]


# Additive margin, in phrases per 1000 words: a tell fires only when the
# draft's measured rate exceeds the user's own baseline rate by more than
# this. A single phrase over baseline is normal; a cluster is the tell.
_PHRASE_MARGIN = 1.5

SIGNIFICANCE = register(
    Tell(
        id="significance_puffery",
        category="rhetorical",
        feature_key="significance_phrase_density",
        handling="surface",
        polarity="avoid",
        description="Undue emphasis on significance/legacy/broader trends.",
        caveat="Surface for review, do not strip: a trailing significance claim "
        "is usually unattributed synthesis. Only flagged above your own rate.",
        measure=features.significance_phrase_density,
        locate=lambda text: _spans(features.SIGNIFICANCE_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)

SUPERFICIAL_ING = register(
    Tell(
        id="superficial_ing",
        category="rhetorical",
        feature_key="superficial_ing_density",
        handling="surface",
        polarity="avoid",
        description="Trailing -ing analysis clauses (highlighting/reflecting/...).",
        caveat="A trailing -ing clause is often unattributed synthesis; reground "
        "or cut rather than delete the clause. Only flagged above your rate.",
        measure=features.superficial_ing_density,
        locate=lambda text: _spans(features.SUPERFICIAL_ING_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)

PEACOCK = register(
    Tell(
        id="peacock_language",
        category="rhetorical",
        feature_key="peacock_density",
        handling="surface",
        polarity="avoid",
        description="Promotional / travel-guide peacock language (vibrant, nestled).",
        caveat="Promotional register relative to your own; a writer who runs "
        "lyrical is not flagged for it.",
        measure=features.peacock_density,
        locate=lambda text: _spans(features.PEACOCK_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)

VAGUE_ATTRIBUTION = register(
    Tell(
        id="vague_attribution",
        category="rhetorical",
        feature_key="vague_attribution_density",
        handling="surface",
        polarity="avoid",
        description="Vague attribution / weasel wording (Experts argue, some sources).",
        caveat="Down-weight when a real inline citation is attached; only flagged "
        "above your own measured rate.",
        measure=features.vague_attribution_density,
        locate=lambda text: _spans(features.VAGUE_ATTRIBUTION_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)
