"""Deterministic repair: stray-markup artifacts + vault-aware typography.

Two kinds of fix live here, split by whether the feature can ever be the
user's voice:

* **Markup artifacts** (``oaicite``, ``turn0search``, ``grok-card``,
  ``utm_source=...`` and friends) are never human output, so they are
  stripped unconditionally.
* **Typography** (curly quotes, em-dash density) *is* style. It is only
  normalised when the voice fingerprint shows the user does not write that
  way; a curly-quote- or em-dash-loving user keeps theirs.

Everything here is frontmatter-safe (operates on the body only) and
idempotent (running twice equals running once).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from creek.generate.ai_style._text import tidy_whitespace
from creek.generate.ai_style.sanitize_structure import sanitize_structure

if TYPE_CHECKING:
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint

_FRONTMATTER_RE = re.compile(r"\A(---\n.*?\n---\n)", re.DOTALL)

# --- markup artifacts (always stripped) ------------------------------------
# Each pattern matches an LLM/tool artifact that is never legitimate human
# text. An optional single leading space is consumed so stripping does not
# leave double spaces. Ambiguous unicode (double-dagger U+2021, lenticular
# brackets U+3010/U+3011) is written as escapes to satisfy the linter.
_ARTIFACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r" ?:contentReference\[oaicite:\d+\]\{index=\d+\}"),
    re.compile(r" ?\[oai_citation:[^\]]*\]\([^)]*\)"),
    re.compile(r" ?oai_citation:\d+\u2021\S*"),
    # Colon-prefixed only, so it never fires inside legitimate identifiers
    # like a DOM "contentReference" API mentioned in prose.
    re.compile(r" ?:contentReference\b"),
    re.compile(r" ?(?:cite)?turn\d+(?:search|image|news|file|view)\d+"),
    re.compile(r" ?\u3010[^\u3011]*\u3011"),  # lenticular brackets
    re.compile(r" ?\[(?:attached_file|web):\d+\]"),
    # grok-card tags self-close in practice; strip both forms defensively.
    re.compile(r" ?<grok-card\b[^>]*>"),
    re.compile(r" ?</grok-card>"),
    re.compile(r" ?\[\]\(grok_render_citation_card_json=[^)]*\)"),
    re.compile(r" ?grok_render_citation_card_json=\{[^}]*\}"),
    re.compile(r' ?\(\{"attribution":.*?\}\)'),
)

# AI tracking params. Captures the leading separator and any following "&"
# so the param can be removed from any position without corrupting the URL
# (see _strip_utm). ``openai`` is matched with or without a TLD.
_UTM_RE = re.compile(
    r"([?&])(?:utm_source=(?:chatgpt\.com|openai(?:\.com)?|copilot\.com)"
    r"|referrer=grok\.com)\b(&)?",
)


def _strip_utm(match: re.Match[str]) -> str:
    """Return the replacement for a matched AI tracking param.

    Preserves a valid query string regardless of the param's position:
    a first param keeps the ``?`` (dropping the following ``&``); a
    middle/last/only param is removed separator and all.

    Args:
        match: A match of :data:`_UTM_RE`.

    Returns:
        ``"?"`` when the param was first of several, else ``""``.
    """
    leading, following_amp = match.group(1), match.group(2)
    if leading == "?" and following_amp:
        return "?"
    return ""


# --- typography ------------------------------------------------------------
_CURLY_TO_STRAIGHT = {
    0x2018: "'",
    0x2019: "'",
    0x201C: '"',
    0x201D: '"',
}
_SPACED_EM_DASH_RE = re.compile(r"\s\u2014\s")  # the formulaic spaced em-dash


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split *text* into its YAML frontmatter block and body.

    Args:
        text: The full text, possibly beginning with a ``---`` fence.

    Returns:
        ``(frontmatter, body)``; the frontmatter is ``""`` when absent.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return "", text
    return match.group(1), text[match.end() :]


def strip_markup_artifacts(body: str) -> str:
    """Remove LLM/tool markup artifacts and AI tracking params from *body*.

    Args:
        body: Prose to clean (frontmatter already removed).

    Returns:
        The body with every known artifact and AI ``utm_source`` /
        ``referrer`` param removed, and whitespace tidied.
    """
    for pattern in _ARTIFACT_PATTERNS:
        body = pattern.sub("", body)
    body = _UTM_RE.sub(_strip_utm, body)
    return tidy_whitespace(body)


def _uses_feature(
    fingerprint: VoiceFingerprint,
    feature_key: str,
    config: AIStyleConfig,
) -> bool:
    """Return whether the user demonstrably uses *feature_key* in their voice.

    A feature counts as "the user's" only when the fingerprint is not thin
    and its measured rate is above zero. A thin or silent fingerprint means
    we cannot vouch for it, so the caller normalises generically.

    Args:
        fingerprint: The user's voice fingerprint.
        feature_key: The typography feature to check.
        config: AI-style configuration (thin-corpus threshold).

    Returns:
        ``True`` when the feature is part of the user's measured voice.
    """
    if fingerprint.is_thin(config.min_fingerprint_fragments):
        return False
    rate = fingerprint.rate_for(feature_key)
    return rate is not None and rate > 0.0


def normalize_typography(
    body: str,
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> str:
    """Normalise curly quotes and em-dash emphasis against the user's grain.

    Curly quotes are straightened, and the formulaic spaced em-dash is
    demoted to a comma, **only** when the fingerprint shows the user does
    not write that way. A user whose own writing uses curly quotes or
    em-dashes keeps them untouched.

    Args:
        body: Prose to normalise.
        fingerprint: The user's voice fingerprint.
        config: AI-style configuration.

    Returns:
        The normalised body.
    """
    if not _uses_feature(fingerprint, "curly_quote_density", config):
        body = body.translate(_CURLY_TO_STRAIGHT)
    if not _uses_feature(fingerprint, "em_dash_density", config):
        body = _SPACED_EM_DASH_RE.sub(", ", body)
    return body


def sanitize(
    text: str,
    *,
    fingerprint: VoiceFingerprint,
    config: AIStyleConfig,
) -> str:
    """Frontmatter-safe, idempotent sanitizer: artifacts, structure, typography.

    Splits off any YAML frontmatter, strips markup artifacts and structural
    LLM tells, normalises typography against the voice fingerprint, and
    recombines.

    Args:
        text: The full text (body, optionally with frontmatter).
        fingerprint: The user's voice fingerprint.
        config: AI-style configuration.

    Returns:
        The sanitized text with frontmatter preserved verbatim.
    """
    front, body = _split_frontmatter(text)
    body = strip_markup_artifacts(body)
    body = sanitize_structure(body)
    body = normalize_typography(body, fingerprint=fingerprint, config=config)
    return front + body
