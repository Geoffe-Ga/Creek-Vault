"""Markdown outline parsing for multi-section drafts.

``creek draft --seed-topic`` accepts a single phrase. Larger essays carry
structure — an introduction, a few claims, a synthesis. The
``--seed-outline`` (a file path) and ``--seed-outline-text`` (inline)
flags let the operator hand that structure in directly, and the draft
pipeline composes each section independently with its own detected
ontology profile, per-dimension retrieval, and ontology-twist
composition, then stitches the sections together with a transition-only
smoothing pass.

This module is the pure, LLM-free half of that feature:

* :class:`OutlineSection` — one header plus the body text beneath it.
* :func:`parse_outline` — split a markdown document into ordered
  sections. Every ATX header opens a section; its body runs until the
  next header of any level. The header's depth is recorded as the
  section level so callers can tell a ``#`` title from a ``##`` claim,
  but each header — at whatever depth — composes as its own section so
  the worked example (a ``#`` title plus three ``##`` claims) yields the
  four sections the operator wrote.
* :class:`OutlineParseError` — raised when the document carries no
  markdown headers (the #354 acceptance criterion: "an outline with no
  markdown headers is rejected with a clear error").
* :func:`format_stitch_directive` / :func:`build_stitch_prompt` — render
  the connective-tissue-only smoothing prompt. The directive forbids
  paraphrasing or inventing section content; the stitch pass may only add
  transitions.

The orchestration that drives detect → retrieve → compose → stitch lives
in :mod:`creek.generate.drafts` so this module stays import-side-effect
free and free of any provider dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ATX headers only: 1-6 leading hashes, at least one space, then text.
# Setext (underline) headers and bare ``#tag`` runs are intentionally
# excluded — the latter has no space after the hashes and is body text.
_HEADER_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")


class OutlineParseError(ValueError):
    """Raised when an outline carries no markdown headers.

    The CLI surfaces the message verbatim so the operator learns that
    ``--seed-outline`` needs at least one ``#`` header to delimit a
    section, rather than silently drafting a single unstructured blob.
    """


@dataclass(frozen=True)
class OutlineSection:
    """One outline section: a header plus the body text beneath it.

    Attributes:
        heading: The header text with the leading ``#`` markers stripped.
        level: The header depth (number of leading hashes, ``1``-``6``).
        body: The text beneath the header up to the next header of any
            level. Empty string when the header has no body before the
            following header.
    """

    heading: str
    level: int
    body: str

    @property
    def seed_text(self) -> str:
        """Return the heading + body joined, for ontology detection.

        The detector (:func:`creek.classify.prompt.detect_ontology`)
        reads a free-form prompt; concatenating the heading with its
        body gives the richest signal for the section while keeping the
        heading's framing in play.

        Returns:
            ``"{heading}\\n\\n{body}"`` when a body exists, otherwise the
            heading alone.
        """
        if not self.body.strip():
            return self.heading.strip()
        return f"{self.heading.strip()}\n\n{self.body.strip()}"


def _match_header(line: str) -> tuple[int, str] | None:
    """Return ``(level, heading_text)`` for an ATX header line, else ``None``."""
    match = _HEADER_RE.match(line)
    if match is None:
        return None
    hashes, text = match.groups()
    return len(hashes), text.strip()


@dataclass
class _OpenSection:
    """Mutable accumulator for a section under construction."""

    heading: str
    level: int
    body_lines: list[str]

    def finish(self) -> OutlineSection:
        """Freeze the accumulator into an immutable :class:`OutlineSection`."""
        body = "\n".join(self.body_lines).strip()
        return OutlineSection(heading=self.heading, level=self.level, body=body)


def parse_outline(text: str) -> list[OutlineSection]:
    """Split *text* into ordered :class:`OutlineSection` records.

    Every ATX header (``#``..``######`` followed by a space) opens a new
    section. The section's body is the text beneath the header up to the
    next header of *any* level — so a ``#`` title and the three ``##``
    claims beneath it compose as four sections, each with its own
    detected ontology, in document order.

    Text appearing before the first header is dropped: it belongs to no
    section. ``#tag`` runs (no space after the hashes) and setext
    underline headers are treated as body text, not section breaks.

    Args:
        text: The raw markdown outline (file contents or inline string).

    Returns:
        The sections in document order. Always non-empty on success.

    Raises:
        OutlineParseError: When *text* contains no ATX markdown headers.
    """
    sections: list[OutlineSection] = []
    current: _OpenSection | None = None
    for line in text.splitlines():
        header = _match_header(line)
        if header is None:
            if current is not None:
                current.body_lines.append(line)
            continue
        if current is not None:
            sections.append(current.finish())
        level, heading = header
        current = _OpenSection(heading=heading, level=level, body_lines=[])
    if current is not None:
        sections.append(current.finish())
    if not sections:
        msg = (
            "Outline contains no markdown headers; --seed-outline needs at "
            "least one '#' header to delimit a section."
        )
        raise OutlineParseError(msg)
    return sections


def format_stitch_directive(section_count: int) -> str:
    """Render the connective-tissue-only directive for the stitch pass.

    The stitch pass smooths transitions between independently-composed
    sections. It must not paraphrase or invent section content — the
    #354 acceptance criterion. The directive states that contract
    explicitly so the LLM treats it as load-bearing.

    Args:
        section_count: How many sections the draft has; named so the
            model knows exactly how many bodies must survive unchanged.

    Returns:
        A markdown ``## Stitch directive`` block.
    """
    return (
        "## Stitch directive\n"
        f"The draft below is assembled from {section_count} sections that "
        "were composed independently. Your only job is to add connective "
        "tissue — short transition sentences between sections — so the "
        "essay reads as one continuous piece. Do NOT paraphrase, rewrite, "
        "summarise, condense, reorder, or invent any section content. "
        "Every section's substance must survive verbatim; you may add "
        "bridging sentences between sections and nothing more. Keep all "
        f"{section_count} sections, in their original order."
    )


def build_stitch_prompt(
    sections: list[tuple[str, str]],
    *,
    voice_core: str,
    voice_targets: str = "",
) -> str:
    """Assemble the stitch-pass prompt from per-section bodies.

    Args:
        sections: Ordered ``(heading, body)`` pairs — each body is an
            already-composed section that must survive the stitch pass
            unchanged save for added transitions.
        voice_core: Optional voice-core description prepended to the
            prompt; empty string to omit.
        voice_targets: Optional FEAT-040.8 ``## Voice targets`` preamble
            inserted after the voice core so the smoothing pass keeps to the
            user's measured voice; empty string to omit.

    Returns:
        The full stitch prompt: voice core (if any), the voice targets (if
        any), the directive, the labelled section bodies, and the ask.
    """
    parts: list[str] = []
    if voice_core.strip():
        parts.append(f"## Voice core\n{voice_core.strip()}")
    if voice_targets.strip():
        parts.append(voice_targets.strip())
    parts.append(format_stitch_directive(len(sections)))
    rendered = "\n\n".join(
        f"### {heading}\n{body.strip()}" for heading, body in sections
    )
    parts.extend(
        (
            f"## Sections\n\n{rendered}",
            "## Ask\n"
            "Return the full essay with smooth transitions between the "
            "sections above. Add only connective tissue; leave each "
            "section's content intact.",
        ),
    )
    return "\n\n".join(parts)


__all__ = [
    "OutlineParseError",
    "OutlineSection",
    "build_stitch_prompt",
    "format_stitch_directive",
    "parse_outline",
]
