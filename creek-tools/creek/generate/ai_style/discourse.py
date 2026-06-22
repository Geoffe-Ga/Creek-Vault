"""Discourse / structure detectors (FEAT-040.7): vault-relative tells.

The structural AI tells: negative parallelisms, rule-of-three padding, the
rigid "Challenges / Future Prospects" formula, list-title-as-entity leads,
knowledge-cutoff disclaimers, didactic disclaimers, section summaries, and
collaborative-comm boilerplate. They point at structural habits rather than
single words, so like the rhetorical tells they are **surface-only**: the
scanner flags them for review/rewrite, never auto-strips.

Most tells are **vault-relative** — a writer who genuinely loves a triad or
opens with "Overall," has a high baseline and is not flagged for it. Two are
the exception: :data:`KNOWLEDGE_CUTOFF` and :data:`PROVENANCE_TELL` fire
regardless of the fingerprint (``margin`` and ``generic_prior`` both ``0``)
because phrases like "As of my last knowledge update" or "from one of my
journals" are never desirable in finished prose. The knowledge-cutoff caveat
carries a fabrication-risk hint; the provenance caveat steers the draft to
state the idea, not its journal/note provenance.

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


def _spans(pattern: re.Pattern[str], text: str, *, group: int | str = 0) -> list[Span]:
    """Return the spans of *pattern*'s *group* for every match in *text*."""
    return [Span(m.start(group), m.end(group)) for m in pattern.finditer(text)]


# Additive margin, in phrases per 1000 words: a tell fires only when the
# draft's measured rate exceeds the user's own baseline by more than this.
# A single instance over baseline is normal prose; a cluster is the tell.
_PHRASE_MARGIN = 1.5
# Rule-of-three is strongly gated: triads are common in good human writing,
# so require a large excess over the user's own measured triad rate.
_TRIAD_MARGIN = 3.0
# Distinctive structural formulas (challenges section, list-title lead,
# section summary, comment boilerplate) need only a modest excess.
_STRUCTURE_MARGIN = 0.5

NEGATIVE_PARALLELISM = register(
    Tell(
        id="negative_parallelism",
        category="discourse",
        feature_key="negative_parallelism_density",
        handling="surface",
        polarity="avoid",
        description="Negative parallelism (not only X but Y; it's not X, it's Y).",
        caveat="A single antithesis is a normal rhetorical move; only flagged "
        "as a cluster above your own measured rate.",
        measure=features.negative_parallelism_density,
        locate=lambda text: _spans(features.NEGATIVE_PARALLELISM_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)

RULE_OF_THREE_PADDING = register(
    Tell(
        id="rule_of_three_padding",
        category="discourse",
        feature_key="rule_of_three_rate",
        handling="surface",
        polarity="avoid",
        description="Rule-of-three triads used as padding (a, b, and c).",
        caveat="Triads are common in good human prose; strongly gated, only "
        "flagged well above your own measured triad rate. If you love a "
        "triad, it is your voice.",
        measure=features.rule_of_three_rate,
        locate=lambda text: _spans(features.TRIAD_RE, text),
        margin=_TRIAD_MARGIN,
    ),
)

CHALLENGES_SECTION = register(
    Tell(
        id="challenges_section",
        category="discourse",
        feature_key="challenges_section_density",
        handling="surface",
        polarity="avoid",
        description="Rigid 'Despite its X, Y faces challenges' outline formula.",
        caveat="The mere mention of challenges is fine; the tell is the rigid "
        "puff-then-challenge-then-speculative-future formula. Only flagged "
        "above your own rate.",
        measure=features.challenges_section_density,
        locate=lambda text: _spans(features.CHALLENGES_SECTION_RE, text),
        margin=_STRUCTURE_MARGIN,
    ),
)

LIST_TITLE_LEAD = register(
    Tell(
        id="list_title_lead",
        category="discourse",
        feature_key="list_title_lead_density",
        handling="surface",
        polarity="avoid",
        description="A list / non-proper-noun title defined as a standalone entity.",
        caveat="Defining 'List of X' as 'is a list of ...' is an AI lead habit; "
        "only flagged above your own rate.",
        measure=features.list_title_lead_density,
        locate=lambda text: _spans(features.LIST_TITLE_LEAD_RE, text, group="phrase"),
        margin=_STRUCTURE_MARGIN,
    ),
)

KNOWLEDGE_CUTOFF = register(
    Tell(
        id="knowledge_cutoff",
        category="discourse",
        feature_key="knowledge_cutoff_density",
        handling="surface",
        polarity="avoid",
        description="Knowledge-cutoff / not-documented disclaimer leaking from an LLM.",
        caveat="High fabrication risk: 'as of my last knowledge update', "
        "'likely supports', 'maintains a low profile' signal fabricated or "
        "unverifiable claims. Never desirable in finished prose, so flagged "
        "regardless of your fingerprint — verify the claim or cut it.",
        measure=features.knowledge_cutoff_density,
        locate=lambda text: _spans(features.KNOWLEDGE_CUTOFF_RE, text),
        # Never legitimate: fire on any occurrence, independent of the voice.
        generic_prior=0.0,
        margin=0.0,
    ),
)

PROVENANCE_TELL = register(
    Tell(
        id="provenance_tell",
        category="discourse",
        feature_key="provenance_density",
        handling="surface",
        polarity="avoid",
        description="First-person sourcing announcement (my journals, one of them).",
        caveat="The draft should weave source fragments in as the owner's own "
        "present-tense thinking, never narrate that something came from a "
        "journal/note/entry or that it is quoting itself. Never desirable in "
        "finished prose, so flagged regardless of your fingerprint — state the "
        "idea, not its provenance.",
        measure=features.provenance_density,
        locate=lambda text: _spans(features.PROVENANCE_RE, text),
        # Never legitimate: fire on any occurrence, independent of the voice.
        generic_prior=0.0,
        margin=0.0,
    ),
)

DIDACTIC_DISCLAIMER = register(
    Tell(
        id="didactic_disclaimer",
        category="discourse",
        feature_key="didactic_disclaimer_density",
        handling="surface",
        polarity="avoid",
        description="Didactic disclaimers (it's important to note, may vary).",
        caveat="An occasional caveat is normal; only flagged as a cluster above "
        "your own measured rate.",
        measure=features.didactic_disclaimer_density,
        locate=lambda text: _spans(features.DIDACTIC_DISCLAIMER_RE, text),
        margin=_PHRASE_MARGIN,
    ),
)

SECTION_SUMMARY = register(
    Tell(
        id="section_summary",
        category="discourse",
        feature_key="section_summary_density",
        handling="surface",
        polarity="avoid",
        description="Sentence-initial section summaries (In summary, Overall, ...).",
        caveat="A closing summary is sometimes warranted; only flagged above your "
        "own rate of restating the thesis.",
        measure=features.section_summary_density,
        locate=lambda text: _spans(features.SECTION_SUMMARY_RE, text, group="phrase"),
        margin=_STRUCTURE_MARGIN,
    ),
)

COMM_BOILERPLATE = register(
    Tell(
        id="comm_boilerplate",
        category="discourse",
        feature_key="comm_boilerplate_density",
        handling="surface",
        polarity="avoid",
        description="Collaborative-comm boilerplate (I hope this helps, Certainly!).",
        caveat="Comment context only; chat pleasantries and AfC/submission "
        "boilerplate that leaked into prose. Only flagged above your own rate.",
        measure=features.comm_boilerplate_density,
        locate=lambda text: _spans(features.COMM_BOILERPLATE_RE, text),
        contexts=frozenset({"comment"}),
        margin=_STRUCTURE_MARGIN,
    ),
)

# --- Signature tell (#635): an authentic feature to reinforce, not scrub ---
# Declared ``signature`` because its natural concern is *under*-use: a draft
# that flattens the user's punchy single-sentence-paragraph rhythm into dense
# blocks has lost their voice. Per-user polarity derivation still governs it —
# it only fires as a signature once the user's own one-line-fragment rate
# clears the signature threshold, so it never penalises a writer who does not
# use the rhythm.
ONE_LINE_FRAGMENT = register(
    Tell(
        id="one_line_fragment",
        category="discourse",
        feature_key="one_line_fragment_density",
        handling="surface",
        polarity="signature",
        description="Short single-sentence paragraphs (the punchy one-line beat).",
        caveat="A signature rhythm, not a defect: flagged only when you "
        "characteristically use it and a draft strips it out.",
        measure=features.one_line_fragment_density,
        # Under-use has no single location; a doc-level finding is emitted.
        locate=lambda _text: [],
    ),
)
