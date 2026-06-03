"""No-fabrication cohesion pass for single-topic drafts (issue #518).

Single-topic drafts often read as disjointed — sections sit next to each
other with abrupt seams and no connective tissue. This module adds an
*opt-in* cohesion pass that asks the LLM to smooth those seams by inserting
transitions, under a hard anti-fabrication constraint: it may introduce no
new named entities, numbers, claims, events, or facts. Transitions only.

The load-bearing, deterministic part is the **entity-preservation guard**
(:func:`is_entity_preserving`): a pure function that compares the set of
preservable tokens — proper nouns, numbers, and the owner's bespoke
ontology terms — in the pre-cohesion text against the post-cohesion text.
If the post text introduces any token absent before, the cohesion output
is rejected and the caller falls back to the original body. Adding bridge
words is fine; adding a new entity/number/fact is not.

The pass is **default-off**: it runs only when explicitly enabled and a
cohesion LLM is wired (it requires the LLM, so the ``--no-llm`` path skips
it entirely). It mirrors the spirit of
:func:`creek.generate.outline.format_stitch_directive` (content-frozen
transitions) but allows light cross-seam smoothing of the owner's own
voice — still no new content.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

#: Callable ``(prompt) -> body`` for the cohesion LLM hop. Reuses the draft
#: LLM's plain-string contract so the same provider seam (and the same test
#: stubs) drive it.
CohesionLLM = Callable[[str], str]

#: Callable ``(body) -> findings`` for the post-cohesion grounding re-check.
#: Returns a non-empty sequence when the smoothed body smuggled in an
#: ungrounded first-person claim (#515); the pass then falls back.
GroundingCheck = Callable[[str], Sequence[object]]


# A token worth preserving is either a number (a run of digits, optionally
# with internal separators) or a "proper-noun" word: a capitalized word that
# is NOT merely capitalized because it opens a sentence. We capture every
# capitalized word here and discard sentence-initial ones in a second pass so
# transition openers ("However,") never register as entities.
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?:;])\s+|\n+")


def _capitalized_proper_nouns(text: str) -> set[str]:
    """Return lowercased capitalized words that are not sentence-initial.

    A word capitalized only because it opens a sentence (or the whole text)
    is ordinary prose, not a named entity — excluding it keeps transition
    openers like "However," from registering as fabricated entities. Every
    other capitalized word is treated as a proper noun to preserve.
    """
    proper: set[str] = set()
    for segment in _SENTENCE_BOUNDARY_RE.split(text):
        stripped = segment.strip()
        if not stripped:
            continue
        words = _WORD_RE.findall(stripped)
        # Skip the first word of each sentence: its capital may be positional.
        for word in words[1:]:
            if word[0].isupper():
                proper.add(word.lower())
    return proper


def extract_preservable_tokens(
    text: str,
    *,
    bespoke_terms: Sequence[str] = (),
) -> set[str]:
    """Return the set of fabrication-sensitive tokens in *text*.

    The set is the union of three families, all lowercased for a
    case-insensitive comparison:

    * **Numbers** — any run of digits. A new number is a new fact (a year,
      a count, an age) and must not appear post-cohesion if it was absent.
    * **Proper nouns** — capitalized words that are not merely
      sentence-initial (see :func:`_capitalized_proper_nouns`). Names,
      places, titles, brands.
    * **Bespoke ontology terms** — the owner's vocabulary (Fragment,
      Resonance, Thread, Eddy, Praxis, …) supplied by the caller, matched
      case-insensitively on word boundaries even when lowercased in prose.

    Args:
        text: The body to scan.
        bespoke_terms: Owner ontology terms to treat as preservable even
            when they appear lowercased.

    Returns:
        A lowercased token set suitable for set-difference comparison.
    """
    tokens = _capitalized_proper_nouns(text)
    tokens.update(_NUMBER_RE.findall(text))
    lowered = text.lower()
    for term in bespoke_terms:
        term_lc = term.lower()
        if re.search(rf"\b{re.escape(term_lc)}\b", lowered):
            tokens.add(term_lc)
    return tokens


def is_entity_preserving(
    *,
    pre: str,
    post: str,
    bespoke_terms: Sequence[str] = (),
) -> bool:
    """Return whether *post* introduces no new preservable token over *pre*.

    The deterministic, LLM-free heart of the cohesion guard. The cohesion
    pass may freely *add transition words* and may even *drop* an entity,
    but it must never **introduce** a proper noun, number, or bespoke
    ontology term that the pre-cohesion text did not already contain — that
    would be a fabricated named entity, date, or fact.

    Args:
        pre: The body before the cohesion rewrite.
        post: The body the cohesion LLM returned.
        bespoke_terms: Owner ontology terms to guard alongside proper nouns
            and numbers.

    Returns:
        ``True`` when ``post``'s preservable-token set is a subset of
        ``pre``'s (transitions-only); ``False`` when ``post`` added at least
        one new token.
    """
    pre_tokens = extract_preservable_tokens(pre, bespoke_terms=bespoke_terms)
    post_tokens = extract_preservable_tokens(post, bespoke_terms=bespoke_terms)
    return post_tokens <= pre_tokens


def format_cohesion_directive() -> str:
    """Render the transitions-only directive for the cohesion pass.

    Mirrors :func:`creek.generate.outline.format_stitch_directive` but for a
    single-topic body: it permits light cross-seam smoothing of the owner's
    own voice while forbidding any new content. The anti-fabrication
    sentence is load-bearing — the deterministic guard enforces it, and the
    directive tells the model the rule up front so it does not waste a hop
    producing output that will be rejected.

    Returns:
        A markdown ``## Cohesion directive`` block.
    """
    return (
        "## Cohesion directive\n"
        "The draft below is a single essay whose sections were composed "
        "with abrupt seams between them. Your only job is to make it read "
        "as one continuous piece: add short transition sentences between "
        "sections and lightly smooth the connective phrasing in the owner's "
        "own voice. Introduce NO new named entities, numbers, dates, claims, "
        "events, or facts — transitions only. Do not add a person, place, "
        "title, or number that is not already present. Keep every existing "
        "sentence's substance; you may add bridging phrases and nothing more."
    )


def build_cohesion_prompt(body: str, *, voice_core: str = "") -> str:
    """Assemble the cohesion-pass prompt from a composed *body*.

    Args:
        body: The already-composed single-topic draft body to smooth.
        voice_core: Optional voice-core description prepended so the
            transitions are smoothed in the owner's voice; empty to omit.

    Returns:
        The full prompt: optional voice core, the cohesion directive, then
        the body to smooth.
    """
    parts: list[str] = []
    if voice_core.strip():
        parts.append(f"## Voice core\n{voice_core.strip()}")
    parts.extend(
        (format_cohesion_directive(), f"## Draft to smooth\n{body}"),
    )
    return "\n\n".join(parts)


def run_cohesion_pass(
    body: str,
    *,
    cohesion_llm: CohesionLLM | None,
    enabled: bool,
    grounding_check: GroundingCheck,
    voice_core: str = "",
    bespoke_terms: Sequence[str] = (),
) -> str:
    """Run the opt-in cohesion pass, returning the smoothed or original body.

    The pass is conservative by construction — any failure mode falls back
    to *body* unchanged:

    #. **Disabled / no LLM.** When ``enabled`` is ``False`` or
       ``cohesion_llm`` is ``None`` (the deterministic ``--no-llm`` path),
       the original body is returned and the LLM is never called.
    #. **Empty output.** A blank cohesion response falls back rather than
       blanking the draft.
    #. **Fabricated entity.** When :func:`is_entity_preserving` rejects the
       output — a new proper noun, number, or bespoke term appeared — the
       original body is returned.
    #. **Ungrounded claim.** When *grounding_check* flags the smoothed body
       (a smuggled first-person biographical claim, #515), the original body
       is returned.

    Args:
        body: The composed single-topic draft body.
        cohesion_llm: The cohesion LLM seam, or ``None`` to skip the pass.
        enabled: The opt-in flag; the pass is a no-op when ``False``.
        grounding_check: Callable re-running the biographical grounding flag
            on the smoothed body; a non-empty return triggers fallback.
        voice_core: Optional voice-core text for the prompt.
        bespoke_terms: Owner ontology terms guarded alongside proper nouns
            and numbers.

    Returns:
        The smoothed body when every guard passes; otherwise *body*.
    """
    if not enabled or cohesion_llm is None:
        return body
    smoothed = cohesion_llm(build_cohesion_prompt(body, voice_core=voice_core)).strip()
    if not smoothed:
        return body
    if not is_entity_preserving(pre=body, post=smoothed, bespoke_terms=bespoke_terms):
        return body
    if grounding_check(smoothed):
        return body
    return smoothed
