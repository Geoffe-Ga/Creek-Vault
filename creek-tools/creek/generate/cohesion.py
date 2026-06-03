"""No-fabrication cohesion pass for single-topic drafts.

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
import sys
from collections.abc import Callable, Sequence

#: Callable ``(prompt) -> body`` for the cohesion LLM hop. Reuses the draft
#: LLM's plain-string contract so the same provider seam (and the same test
#: stubs) drive it.
CohesionLLM = Callable[[str], str]

#: Callable ``(body) -> findings`` for the post-cohesion grounding re-check.
#: Returns a non-empty sequence when the smoothed body smuggled in an
#: ungrounded first-person claim; the pass then falls back.
GroundingCheck = Callable[[str], Sequence[object]]


# A token worth preserving is either a number (a run of digits, optionally
# with internal separators) or a "proper-noun" word: a capitalized word that
# is NOT merely capitalized because it opens a sentence. We capture every
# capitalized word here and discard sentence-initial ones in a second pass so
# transition openers ("However,") never register as entities.
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?:;])\s+|\n+")
#: A markdown header line (one to six leading ``#`` and the following text).
#: Header text is structural, not prose, so it is excluded from proper-noun
#: extraction — see :func:`_capitalized_proper_nouns`.
_HEADER_LINE_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+.*$", re.MULTILINE)
#: Count of ``## `` (and deeper) top-level section headers in a body. Two or
#: more means a multi-section outline draft, which the cohesion pass must skip.
_SECTION_HEADER_RE = re.compile(r"^[ \t]*#{2,6}[ \t]+\S", re.MULTILINE)
#: Minimum top-level section headers that mark a body as a multi-section
#: outline draft (and so out of scope for the single-topic cohesion pass).
_MULTI_SECTION_THRESHOLD = 2


def _strip_header_lines(text: str) -> str:
    """Blank out markdown header lines so their words are not scanned.

    A header like ``## Rising Tide`` is structural, not prose; its title-cased
    words ("Rising", "Tide") would otherwise register as fabricated proper
    nouns when only one of pre/post happens to contain the header. Replacing
    each header line with an empty line (preserving line count) removes that
    structural noise before proper-noun extraction.
    """
    return _HEADER_LINE_RE.sub("", text)


def _capitalized_proper_nouns(text: str) -> set[str]:
    """Return lowercased capitalized words that are not sentence-initial.

    A word capitalized only because it opens a sentence (or the whole text)
    is ordinary prose, not a named entity — excluding it keeps transition
    openers like "However," from registering as fabricated entities. Every
    other capitalized word is treated as a proper noun to preserve.

    Markdown header lines (``^#{1,6}\\s+…``) are stripped first so structural,
    title-cased header words are never captured as preservable proper nouns.
    """
    proper: set[str] = set()
    for segment in _SENTENCE_BOUNDARY_RE.split(_strip_header_lines(text)):
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
    capitalized_terms: Sequence[str] = (),
) -> set[str]:
    """Return the set of fabrication-sensitive tokens in *text*.

    The set is the union of four families, all lowercased for a
    case-insensitive comparison:

    * **Numbers** — any run of digits. A new number is a new fact (a year,
      a count, an age) and must not appear post-cohesion if it was absent.
    * **Proper nouns** — capitalized words that are not merely
      sentence-initial (see :func:`_capitalized_proper_nouns`). Names,
      places, titles, brands. Markdown header lines are excluded so
      structural header words are not captured.
    * **Distinctive bespoke terms** — the owner's *distinctive* vocabulary
      (APTITUDE, Praxis, Wavelength, Resonance, Fragment, …) supplied via
      ``bespoke_terms``, matched **case-insensitively** on word boundaries
      even when lowercased in prose, because lowercasing them in prose is
      still an unambiguous ontology reference.
    * **Capitalized common terms** — ontology words that are *also* common
      English words (Thread, Eddy and their plurals) supplied via
      ``capitalized_terms``, matched **case-sensitively** so only their
      Capitalized ontological form is guarded. A lowercase "eddies of
      thought" transition is ordinary prose and is intentionally NOT
      captured here; a capitalized ``Eddy`` is caught here (and would also
      be caught as a proper noun).

    Known limitations (intentional, documented):

    * **Allcaps terms.** A distinctive allcaps term like ``APTITUDE`` is
      captured both as a bespoke term and (incidentally) by the proper-noun
      pass; this double-capture is harmless and intentional — allcaps tokens
      are always treated as preservable.
    * **Compound proper nouns.** A multi-word name like "New York" is split
      into separate tokens ("new", "york"). The guard therefore preserves
      the *parts* rather than the phrase; introducing either part anew is
      still rejected, so the anti-fabrication contract holds, but the guard
      does not model the compound as a single unit.

    Args:
        text: The body to scan.
        bespoke_terms: Distinctive owner ontology terms to treat as
            preservable even when they appear lowercased (case-insensitive).
        capitalized_terms: Common-word ontology terms guarded only in their
            Capitalized ontological form (case-sensitive).

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
    for term in capitalized_terms:
        if re.search(rf"\b{re.escape(term)}\b", text):
            tokens.add(term.lower())
    return tokens


def is_entity_preserving(
    *,
    pre: str,
    post: str,
    bespoke_terms: Sequence[str] = (),
    capitalized_terms: Sequence[str] = (),
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
        bespoke_terms: Distinctive owner ontology terms to guard
            case-insensitively alongside proper nouns and numbers.
        capitalized_terms: Common-word ontology terms guarded only in their
            Capitalized ontological form (case-sensitive).

    Returns:
        ``True`` when ``post``'s preservable-token set is a subset of
        ``pre``'s (transitions-only); ``False`` when ``post`` added at least
        one new token.
    """
    pre_tokens = extract_preservable_tokens(
        pre, bespoke_terms=bespoke_terms, capitalized_terms=capitalized_terms
    )
    post_tokens = extract_preservable_tokens(
        post, bespoke_terms=bespoke_terms, capitalized_terms=capitalized_terms
    )
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


def _warn_cohesion_fallback(reason: str) -> None:
    """Print a brief cohesion-guard fallback diagnostic to stderr.

    Mirrors the ``grounding guard:`` stderr warnings emitted by the draft
    grounding checks so an operator who opted into ``--cohesion`` learns *why*
    the pass had no effect.

    Args:
        reason: A short phrase naming which guard rejected the output.
    """
    print(f"cohesion guard: {reason}; kept pre-cohesion body", file=sys.stderr)


def _is_multi_section(body: str) -> bool:
    """Return whether *body* has 2+ top-level section headers.

    Two or more ``## `` (or deeper) headers mark a multi-section outline
    draft, which carries its own seam stitch and must NOT be cohesion-smoothed
    across its section boundaries (see :func:`run_cohesion_pass`).
    """
    return len(_SECTION_HEADER_RE.findall(body)) >= _MULTI_SECTION_THRESHOLD


def run_cohesion_pass(
    body: str,
    *,
    cohesion_llm: CohesionLLM | None,
    enabled: bool,
    grounding_check: GroundingCheck,
    voice_core: str = "",
    bespoke_terms: Sequence[str] = (),
    capitalized_terms: Sequence[str] = (),
) -> str:
    """Run the opt-in cohesion pass, returning the smoothed or original body.

    **Single-topic contract.** This pass operates on a **single-topic body**.
    It must NOT be used on a multi-section outline draft (the
    :func:`~creek.generate.outline` / ``compose_outline_draft`` path), which
    carries its own content-frozen seam stitch — smoothing across section
    seams here would blur deliberately distinct sections. As a defensive
    guard, a body with two or more top-level ``## `` headers is detected as
    multi-section and the pass no-ops (returning *body* unchanged) with a
    ``cohesion guard: … multi-section`` stderr diagnostic.

    The pass is conservative by construction — any failure mode falls back
    to *body* unchanged:

    #. **Disabled / no LLM.** When ``enabled`` is ``False`` or
       ``cohesion_llm`` is ``None`` (the deterministic ``--no-llm`` path),
       the original body is returned and the LLM is never called.
    #. **Multi-section body.** When the body has 2+ top-level ``## ``
       headers it is an outline draft, out of scope; the pass no-ops before
       the LLM is called.
    #. **Empty output.** A blank cohesion response falls back rather than
       blanking the draft.
    #. **Fabricated entity.** When :func:`is_entity_preserving` rejects the
       output — a new proper noun, number, or bespoke term appeared — the
       original body is returned.
    #. **Ungrounded claim.** When *grounding_check* flags the smoothed body
       (a smuggled first-person biographical claim), the original body
       is returned.

    Each fallback (multi-section skip, empty response, fabricated entity,
    ungrounded claim) emits a brief ``cohesion guard:`` warning on stderr so an
    operator who opted into the pass learns why it had no effect. The return
    contract is unchanged.

    Args:
        body: The composed single-topic draft body.
        cohesion_llm: The cohesion LLM seam, or ``None`` to skip the pass.
        enabled: The opt-in flag; the pass is a no-op when ``False``.
        grounding_check: Callable re-running the biographical grounding flag
            on the smoothed body; a non-empty return triggers fallback.
        voice_core: Optional voice-core text for the prompt.
        bespoke_terms: Distinctive owner ontology terms guarded
            case-insensitively alongside proper nouns and numbers.
        capitalized_terms: Common-word ontology terms guarded only in their
            Capitalized ontological form (case-sensitive).

    Returns:
        The smoothed body when every guard passes; otherwise *body*.
    """
    if not enabled or cohesion_llm is None:
        return body
    if _is_multi_section(body):
        _warn_cohesion_fallback("skipped multi-section body (outline draft)")
        return body
    smoothed = cohesion_llm(build_cohesion_prompt(body, voice_core=voice_core)).strip()
    if not smoothed:
        _warn_cohesion_fallback("empty cohesion response")
        return body
    if not is_entity_preserving(
        pre=body,
        post=smoothed,
        bespoke_terms=bespoke_terms,
        capitalized_terms=capitalized_terms,
    ):
        _warn_cohesion_fallback("cohesion output introduced a new entity/number/term")
        return body
    if grounding_check(smoothed):
        _warn_cohesion_fallback("cohesion output smuggled an ungrounded claim")
        return body
    return smoothed
