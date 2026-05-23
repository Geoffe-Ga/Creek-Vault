"""Shared slug helper for ``creek save`` filenames (FEAT-009 follow-up #223).

Two call sites used to carry near-identical regex slugifiers:

* :func:`creek.classify.privacy_filter._stub_relpath_for` —
  intimate-stub filenames under ``10-Liminal/Compost/intimate-stubs/``.
* :func:`creek.save.writer._compose_base_name` — vault note filenames
  under each :class:`~creek.save.SaveTarget`'s subdirectory.

The patterns happened to produce equivalent output for the existing
inputs but used different regexes, so a future reader patching one
would not know to patch the other. This module owns the single
implementation; both call sites import it.

Leaf module — imports nothing from the rest of :mod:`creek.save`, so
it can be pulled in from anywhere (notably
:mod:`creek.classify.privacy_filter`) without re-introducing a
circular import.
"""

from __future__ import annotations

import re
from typing import Final

_DEFAULT_MAX_LENGTH: Final = 64

_WHITESPACE_RE: Final = re.compile(r"\s+")
_INVALID_CHARS_RE: Final = re.compile(r"[^\w-]+")
_HYPHEN_RUN_RE: Final = re.compile(r"-{2,}")


def slugify_filename(text: str, *, max_length: int = _DEFAULT_MAX_LENGTH) -> str:
    """Return a filesystem-safe slug derived from *text*.

    The pipeline is:

    1. Strip surrounding whitespace.
    2. Collapse internal whitespace runs to a single hyphen.
    3. Drop any character outside ``[\\w-]`` (letters, digits,
       underscore, hyphen).
    4. Collapse consecutive hyphens.
    5. Strip leading / trailing hyphens.
    6. Truncate to *max_length* and strip trailing hyphens again so a
       truncation that lands on a hyphen does not violate
       idempotency — ``slugify_filename(slugify_filename(x)) ==
       slugify_filename(x)`` for any *x*.

    Casing is preserved — callers that want a lowercase slug should
    ``.lower()`` their input before calling.

    Args:
        text: The raw text to slugify.
        max_length: Maximum length of the returned slug. Defaults to
            64 characters, matching the legacy intimate-stub limit.
            Callers that need a longer name (e.g. vault filenames,
            historically 80 characters) should override.

    Returns:
        A slug string suitable for use as a filename stem. May be
        empty if *text* contained no slug-eligible characters; callers
        are responsible for substituting a fallback such as
        ``"untitled"`` when that matters.
    """
    intermediate = _WHITESPACE_RE.sub("-", text.strip())
    cleaned = _INVALID_CHARS_RE.sub("", intermediate)
    collapsed = _HYPHEN_RUN_RE.sub("-", cleaned).strip("-")
    return collapsed[:max_length].rstrip("-")
