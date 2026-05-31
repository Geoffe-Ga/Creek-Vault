"""Deterministic repair: Markdown structure, emoji, and placeholders.

The pairing module for :mod:`creek.generate.ai_style.sanitize_typography`.
creek's vault is **Obsidian markdown**, so markdown headings, bold, italic,
and links are the *correct* target — there is no wikitext conversion to do.
What remains are the meaning-preserving structural tells:

* a whole-body fenced code block wrapping the essay (an LLM "here is your
  article" artifact),
* a thematic break (``---`` / ``***``) sitting directly above a heading,
* canned ``- **Header:** text`` vertical lists (the bold-colon is mechanical
  emphasis the user did not add),
* decorative leading emoji on headings and bullets,
* unfilled placeholders (``INSERT_…``, ``PASTE_…``, ``[Your Name]``,
  ``2025-xx-xx``, ``↩``).

Heading-case rewriting (Title Case → sentence case) is intentionally **not**
done here: safely preserving proper nouns is not deterministic, so that tell
is surfaced by the detector layer rather than auto-rewritten. Everything
here operates on the body only and is idempotent.
"""

from __future__ import annotations

import re

from creek.generate.ai_style._text import tidy_whitespace

# Whole-body fenced code block: ``` or ```lang ... ``` spanning the entire
# text (optionally with surrounding blank lines).
_WHOLE_FENCE_RE = re.compile(
    r"\A\s*```[^\n]*\n(?P<body>.*?)\n```\s*\Z",
    re.DOTALL,
)

# A thematic break line immediately followed by an ATX heading. The leading
# blank line (or start of document) is REQUIRED so this never matches a
# setext heading underline (``Title\n---``), which would silently demote a
# real heading to a paragraph. The boundary is captured and preserved.
_BREAK_BEFORE_HEADING_RE = re.compile(
    r"(?P<lead>\A|\n\n)(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?=#{1,6}[ \t])",
)

# Canned inline-header list item: "- **Header:** text" or "1. **Header**: text".
# A colon after the bold is optionally consumed but not captured; the
# replacement re-adds a single normalised colon from the header text.
_INLINE_HEADER_RE = re.compile(
    r"^(?P<marker>[ \t]*(?:[-*+]|\d+\.)[ \t]+)\*\*(?P<header>[^*\n]+?)\*\*:?[ \t]*",
    re.MULTILINE,
)

# Decorative emoji (symbols + pictographs) at the start of a heading/bullet's
# content. Ranges expressed as escapes to avoid ambiguous-literal lint.
_EMOJI = (
    "\U0001f300-\U0001faff"  # symbols & pictographs, supplemental + extended
    "\u2600-\u27bf"  # misc symbols + dingbats
    "\ufe0f"  # variation selector-16
)
_LEADING_EMOJI_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:#{1,6}[ \t]+|(?:[-*+]|\d+\.)[ \t]+))"
    rf"[{_EMOJI}]+[ \t]*",
    re.MULTILINE,
)

_PLACEHOLDER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-[xX]{2}-[xX]{2}\b"),
    re.compile(r"\b(?:INSERT|PASTE)_[A-Z0-9_]*\b"),
    re.compile(r"\[Your Name\]"),
    re.compile(r"\[Describe[^\]]*\]"),
    re.compile(r"↩"),
)


def unwrap_code_fence(body: str) -> str:
    """Unwrap a fenced code block that wraps the entire body.

    LLMs often emit a whole essay inside a ```` ``` ```` block. When the
    *entire* body is one fence, its contents are the real text.

    Args:
        body: Prose to inspect.

    Returns:
        The fence contents when the whole body is one fenced block, else
        *body* unchanged.
    """
    match = _WHOLE_FENCE_RE.match(body)
    return match.group("body") if match is not None else body


def strip_breaks_before_headings(body: str) -> str:
    """Remove a thematic break line sitting directly above a heading.

    Args:
        body: Prose to clean.

    Returns:
        The body without ``---`` / ``***`` / ``___`` lines that precede a
        heading (a Markdown-output AI habit). The preceding blank line is
        preserved; setext heading underlines are left untouched.
    """
    return _BREAK_BEFORE_HEADING_RE.sub(r"\g<lead>", body)


def flatten_inline_header_lists(body: str) -> str:
    """Strip the mechanical bold-colon from canned inline-header list items.

    ``- **Setup:** do the thing`` becomes ``- Setup: do the thing`` — the
    list survives, but the formulaic boldface the user did not write is
    removed.

    Args:
        body: Prose to normalise.

    Returns:
        The body with inline-header bold removed.
    """
    return _INLINE_HEADER_RE.sub(
        lambda m: f"{m.group('marker')}{m.group('header').rstrip(':').rstrip()}: ",
        body,
    )


def strip_leading_emoji(body: str) -> str:
    """Remove decorative emoji at the start of headings and bullets.

    Args:
        body: Prose to clean.

    Returns:
        The body with leading decorative emoji removed from heading/bullet
        lines (the list marker or heading hashes are preserved).
    """
    return _LEADING_EMOJI_RE.sub(lambda m: m.group("prefix"), body)


def strip_placeholders(body: str) -> str:
    """Remove unfilled LLM placeholders (``INSERT_…``, ``2025-xx-xx``, …).

    Args:
        body: Prose to clean.

    Returns:
        The body with placeholder tokens removed and whitespace tidied.
    """
    for pattern in _PLACEHOLDER_RES:
        body = pattern.sub("", body)
    return tidy_whitespace(body)


def sanitize_structure(body: str) -> str:
    """Apply every structural repair to *body* in order.

    Args:
        body: Prose (frontmatter already removed by the caller).

    Returns:
        The structurally sanitized body. Idempotent.
    """
    body = unwrap_code_fence(body)
    body = strip_breaks_before_headings(body)
    body = flatten_inline_header_lists(body)
    body = strip_leading_emoji(body)
    return strip_placeholders(body)
