"""Structural splitter — decompose a fragment into finer-grain children.

This module implements FEAT-021: the *zoom-in* half of confidence-driven
re-atomization (FEAT-023). Given any fragment that resists classification
because it is too large or polyphonic, :func:`split` returns its structural
children at one level finer granularity.

Level transitions (zoom-in):

    ``document``    -> ``section``    (split at H1/H2 headings)
    ``section``     -> ``subsection`` (split at H3+ headings)
    ``subsection``  -> ``paragraph``  (split at blank-line boundaries)
    ``paragraph``   -> ``sentence``   (configurable sentence tokenizer)
    ``sentence``    -> terminal: returns ``[]``

A document with no headings collapses ``document -> paragraph`` (skipping
the empty section and subsection levels). More generally the operator
never produces zero-content levels: when a transition would yield a
single chunk identical to the parent's body, it cascades through the
finer levels until a real split happens or it bottoms out at sentence.

Idempotence: re-splitting the same fragment produces children with
identical IDs and contents, because child IDs are derived deterministically
from ``(parent_id, level, sibling_index)`` via
:func:`creek.ingest.base.generate_child_fragment_id`.

The twin zoom-out operator was FEAT-022 (aggregator), retired by issue
#1342 (ADR-0011) for having no production caller; persistence of
returned children is FEAT-023 (re-atomization driver). This module
performs neither.

Note on the input type: the spec phrases the signature as
``split(fragment: Fragment) -> list[Fragment]``. In the current model,
:class:`~creek.models.Fragment` carries only frontmatter metadata — the
body text lives on :class:`~creek.ingest.base.IngestedFragment`, which
pairs a ``Fragment`` with its converted Markdown body. Because the spec
requires children to carry "the verbatim text slice from the parent",
the splitter operates on the pair, so the runtime signature is
``split(IngestedFragment) -> list[IngestedFragment]``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from creek.ingest.base import IngestedFragment, generate_child_fragment_id

if TYPE_CHECKING:
    from creek.models import Fragment, FragmentLevel

SentenceTokenizer = Callable[[str], list[str]]
"""A function that splits text into sentence strings.

The default :func:`default_sentence_tokenizer` is a regex heuristic
sufficient for English prose. Callers needing higher fidelity (clinical
text, multi-language) pass their own tokenizer to :func:`split` via the
``sentence_tokenizer`` keyword.
"""

_H1_H2_HEADING: Final = re.compile(r"^(?:#{1,2})[ \t]+(.+?)\s*$", re.MULTILINE)
_H3_PLUS_HEADING: Final = re.compile(r"^(?:#{3,6})[ \t]+(.+?)\s*$", re.MULTILINE)
_BLANK_LINE: Final = re.compile(r"\n[ \t]*\n+")
_SENTENCE_BOUNDARY: Final = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'\]\)]))\s+(?=[A-Z\"'\(\[])",
)
_TITLE_FALLBACK_MAX_LEN: Final = 60

_Piece = tuple[str | None, str]
"""A (heading, body_slice) pair produced by an internal carving step."""


def default_sentence_tokenizer(text: str) -> list[str]:
    """Tokenize prose into sentences using a regex heuristic.

    Splits on sentence-terminating punctuation (``.``, ``!``, ``?``)
    optionally followed by closing brackets or quotes, then whitespace
    and a capital letter or opening bracket/quote. Empty input yields
    an empty list.

    Known failure modes: abbreviations (``Dr. Smith``) and ellipses
    (``... and then``) produce extra splits. Callers needing higher
    fidelity (clinical text, formal prose, multi-language) should pass
    their own tokenizer via :func:`split`'s ``sentence_tokenizer`` kwarg.

    Args:
        text: Raw text to tokenize.

    Returns:
        A list of trimmed sentence strings (no empty entries).
    """
    stripped = text.strip()
    if not stripped:
        return []
    parts = _SENTENCE_BOUNDARY.split(stripped)
    return [piece.strip() for piece in parts if piece.strip()]


_LevelHandler = Callable[["Fragment", str, SentenceTokenizer], list[IngestedFragment]]


def split(
    fragment: IngestedFragment,
    *,
    sentence_tokenizer: SentenceTokenizer = default_sentence_tokenizer,
) -> list[IngestedFragment]:
    """Decompose a fragment into its structural children one level finer.

    Args:
        fragment: Parent fragment paired with its body text. The
            parent's ``fragment.level`` selects the transition.
        sentence_tokenizer: Pluggable tokenizer used for
            ``paragraph -> sentence`` transitions (and any cascades that
            reach the sentence level). Must accept a string and return
            a list of sentence strings.

    Returns:
        Children at the finest non-empty level reachable from the
        parent's level. Empty list when the parent is at ``sentence``
        (terminal), at a zoom-out level (``exchange``, ``burst``,
        ``session``), or when no decomposition is possible.
    """
    parent = fragment.fragment
    handler = _DISPATCH.get(parent.level)
    if handler is None:
        return []
    return handler(parent, fragment.body, sentence_tokenizer)


# ---- Per-level handlers -----------------------------------------------------


def _split_document(
    parent: Fragment,
    body: str,
    tokenizer: SentenceTokenizer,
) -> list[IngestedFragment]:
    """Split a document into sections, cascading when no H1/H2 are present."""
    sections = _carve_by_headings(body, _H1_H2_HEADING)
    if sections:
        return _build_children(parent, sections, "section")
    subsections = _carve_by_headings(body, _H3_PLUS_HEADING)
    if subsections:
        return _build_children(parent, subsections, "subsection")
    return _paragraphs_or_sentences(parent, body, tokenizer)


def _split_section(
    parent: Fragment,
    body: str,
    tokenizer: SentenceTokenizer,
) -> list[IngestedFragment]:
    """Split a section into subsections, cascading when no H3+ are present."""
    subsections = _carve_by_headings(body, _H3_PLUS_HEADING)
    if subsections:
        return _build_children(parent, subsections, "subsection")
    return _paragraphs_or_sentences(parent, body, tokenizer)


def _paragraphs_or_sentences(
    parent: Fragment,
    body: str,
    tokenizer: SentenceTokenizer,
) -> list[IngestedFragment]:
    """Split into paragraphs; cascade to sentences when there's only one."""
    paragraphs = [chunk.strip() for chunk in _BLANK_LINE.split(body) if chunk.strip()]
    if len(paragraphs) > 1:
        pieces: list[_Piece] = [(None, para) for para in paragraphs]
        return _build_children(parent, pieces, "paragraph")
    # Forward the stripped paragraph (not raw body) so a custom tokenizer that
    # skips its own .strip() doesn't receive leading/trailing whitespace.
    cascade_body = paragraphs[0] if paragraphs else body
    return _split_paragraph(parent, cascade_body, tokenizer)


def _split_paragraph(
    parent: Fragment,
    body: str,
    tokenizer: SentenceTokenizer,
) -> list[IngestedFragment]:
    """Split a paragraph into sentences via the configured tokenizer."""
    sentences = tokenizer(body)
    if len(sentences) <= 1:
        return []
    pieces: list[_Piece] = [(None, sentence) for sentence in sentences]
    return _build_children(parent, pieces, "sentence")


_DISPATCH: dict[FragmentLevel, _LevelHandler] = {
    "document": _split_document,
    "section": _split_section,
    "subsection": _paragraphs_or_sentences,
    "paragraph": _split_paragraph,
}
"""Level → handler routing table for :func:`split`; hoisted so callers in a
hot loop don't re-allocate it on every invocation."""


# ---- Carving primitives -----------------------------------------------------


def _carve_by_headings(
    body: str,
    pattern: re.Pattern[str],
) -> list[_Piece]:
    """Slice ``body`` at heading boundaries.

    Each match becomes a ``(heading_text, slice)`` pair where ``slice``
    starts at the heading line itself and runs to (but does not include)
    the next heading. Content before the first heading — the preamble —
    is returned as a leading ``(None, slice)`` pair only when non-empty.

    Returns an empty list if no headings match, so callers can detect
    "no structure at this level" and cascade.
    """
    matches = list(pattern.finditer(body))
    if not matches:
        return []

    pieces: list[_Piece] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        pieces.append((None, preamble))

    for idx, match in enumerate(matches):
        heading_text = match.group(1).strip()
        slice_start = match.start()
        slice_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        pieces.append((heading_text, body[slice_start:slice_end].strip()))

    return pieces


def _build_children(
    parent: Fragment,
    pieces: list[_Piece],
    child_level: FragmentLevel,
) -> list[IngestedFragment]:
    """Construct child ``IngestedFragment`` objects from ``pieces``.

    Children inherit every parent field unchanged except the hierarchy
    fields: ``id`` is derived from ``(parent.id, child_level, index)``,
    ``parent_id`` points to the parent, ``child_ids`` is cleared,
    ``level`` is set, and ``structural_path`` is extended by the child's
    heading (when present).
    """
    children: list[IngestedFragment] = []
    for index, (heading, content) in enumerate(pieces):
        child_id = generate_child_fragment_id(parent.id, child_level, index)
        title = heading or _derive_title_from_content(content)
        structural_path = parent.structural_path.copy()
        if heading:
            structural_path.append(heading)
        child_fragment = parent.model_copy(
            update={
                "id": child_id,
                "title": title,
                "parent_id": parent.id,
                "child_ids": [],
                "level": child_level,
                "structural_path": structural_path,
            },
        )
        children.append(IngestedFragment(fragment=child_fragment, body=content))
    return children


def _derive_title_from_content(content: str) -> str:
    """Return a short title from the first line of ``content``.

    Used when a child has no heading of its own (preamble slices,
    paragraphs, sentences). The carving primitives only ever pass
    non-empty content here, so the first non-empty line always exists.
    Long first lines are truncated with an ellipsis so the title stays
    scannable in a vault index.
    """
    first_line = content.strip().splitlines()[0]
    if len(first_line) <= _TITLE_FALLBACK_MAX_LEN:
        return first_line
    return first_line[: _TITLE_FALLBACK_MAX_LEN - 1].rstrip() + "…"
