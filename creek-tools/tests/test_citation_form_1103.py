"""Converted files cite source by symbol, never by line number (#1103).

A ``module.py:<line>`` pointer in a comment or docstring is a claim no tool
checks and every commit can silently falsify. The banner in
``tests/test_mcp_remote.py`` cited a carve-out that had drifted **+66 lines**
away from the cited span, and the issue's own proposed replacement number was
already stale by a *different* amount before anyone typed it. Refreshing the
number is therefore not the fix: the form is the defect.

**Why this is a regex over a form, not a blacklist of the ten strings that
were wrong.** A literal blacklist goes green the moment someone writes the
*correct* number of the day into the same banner, which is exactly the change
this issue exists to forbid. The guard has to reject ``server.py:186-192`` as
firmly as it rejects ``server.py:120-131``.

**Why each file carries an explicit allow-set instead of ``assert not
found``.** Four in-repo citations were measured accurate and are deliberately
kept, plus one third-party pointer into the pinned MCP SDK that no symbol we
own can express. Enumerating them per span keeps them legal without blinding
the guard to a *new* citation appearing in the same file -- which is what a
whole-file allowlist would do, in precisely the files most likely to regress.
Do not "simplify" this to ``assert not found``: that would delete the four
accurate citations this project chose to keep.

Scope honesty: this covers six files. The repository holds far more citations
of the same form, none of which are verified or claimed safe here. A stale
citation that still *resolves* to a real line -- pointing at the wrong code --
is undetectable by any checker, and that unverifiability is the defect class.

This module is not itself in ``_CONVERTED``: the allow-set strings below are
data describing other files, not citations this file makes.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Final

import pytest

_CITATION_RE: Final = re.compile(
    # ``pkg/mod.py:12``, ``mod.py:30-39`` -- the colon-suffixed form.
    r"[A-Za-z_][\w/.]*\.py:\d+(?:-\d+)?"
    # ``:278`` -- the bare continuation a paragraph uses once it has already
    # named the file. Invisible to any filename-anchored pattern, and two of
    # the converted spans were exactly this.
    r"|``:\d+(?:-\d+)?``"
    # The parenthesised prose form -- "(line N)" / "(lines N-M)". A separate
    # branch because it shares no syntax with the other two: the number is
    # not adjacent to the filename, so a reader sees a symbol reference and
    # assumes it is durable while a line number rides along behind it.
    r"|\(lines?\s+\d+(?:\s*[-\u2013]\s*\d+)?\)"
)
"""Every way this codebase writes "the code is at line N".

Three forms, because a guard that knows only one form makes a *false*
cleanliness assertion about the files it covers -- which is worse than not
covering them, since the allow-set then reads as a verified inventory.
"""

_PACKAGE_ROOT: Final = Path(__file__).resolve().parents[1]

_CONVERTED: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("tests/test_mcp_remote.py", frozenset()),
    ("tests/test_mcp_server.py", frozenset()),
    ("tests/test_mcp_tools.py", frozenset()),
    (
        "creek_mcp/attempt_policy.py",
        # Third-party, into the pinned MCP SDK; no symbol we own can pin it,
        # and it was verified accurate rather than grandfathered.
        frozenset({"mcp/server/fastmcp/utilities/func_metadata.py:96"}),
    ),
    (
        "tests/test_lint_new_checks_privacy.py",
        # Measured accurate and deliberately kept.
        frozenset(
            {
                "creek/generate/state.py:1385-1401",
                "creek/atomize/split.py:251",
                "``:278``",
            }
        ),
    ),
    (
        "tests/test_lint_root_hygiene.py",
        # Measured accurate and deliberately kept.
        frozenset({"skill_size_budget.py:55"}),
    ),
)

_REPLACEMENT_SYMBOLS: Final = (
    "creek_mcp.server._BoundedFastMCP.call_tool",
    "creek_mcp.policy.admitted_ceiling",
    "creek_mcp.auth.is_elevated",
    "creek_mcp.server._register_purge_tools",
    "creek_mcp.server._register_conversation_tools",
    "creek_mcp.server._register_authoring_tools",
    "creek_mcp.tools.draft.draft_tool",
    "creek_mcp.tools.draft.DraftLLMFactory",
    "creek.lint.checks.broken_links.run",
    "creek.classify.review.ReviewQueueGenerator.generate_queue",
    "creek.classify.review.ReviewQueueGenerator._format_fragment_entry",
    "creek.clean.hygiene.StaleReviewScanner.scan",
    "creek.clean.hygiene.StaleReviewScanner._parse_filename_timestamp",
)


@pytest.mark.parametrize(
    ("relpath", "allowed"), _CONVERTED, ids=[row[0] for row in _CONVERTED]
)
def test_the_converted_files_cite_symbols_not_line_numbers(
    relpath: str, allowed: frozenset[str]
) -> None:
    """No file below cites source by line outside its enumerated allow-set.

    Reading raw file text is safe for these six specifically: none of them
    embeds a ``mod.py:<n>`` string as test *data*. Two modules elsewhere in
    the suite do, which is the second reason this guard names its files
    instead of walking the tree.
    """
    text = (_PACKAGE_ROOT / relpath).read_text("utf-8")
    found = {match.group(0) for match in _CITATION_RE.finditer(text)}

    assert found <= allowed, (
        f"{relpath} cites source by line number: {sorted(found - allowed)}. "
        "Name the symbol instead -- line numbers drift silently and then "
        "resolve to the wrong code, which is worse than not resolving (#1103)."
    )


@pytest.mark.parametrize("dotted", _REPLACEMENT_SYMBOLS)
def test_the_replacement_symbols_still_resolve(dotted: str) -> None:
    """Every symbol the converted prose now names is still importable.

    Green before this change and green after -- deliberately. It is not the
    RED half; it is the durability half. A symbol reference fails loudly on a
    **rename**, which is the actual way these pointers rot, and the one thing
    a line number only pretended to do.
    """
    parts = dotted.split(".")

    for stop in range(len(parts), 0, -1):
        try:
            resolved: object = importlib.import_module(".".join(parts[:stop]))
        except ImportError:
            continue

        for attr in parts[stop:]:
            assert hasattr(resolved, attr), (
                f"{dotted} no longer resolves: {attr!r} is gone. Converted "
                "prose names this symbol; rename both together (#1103)."
            )
            resolved = getattr(resolved, attr)
        return

    msg = f"no importable module prefix in {dotted!r}"
    raise AssertionError(msg)
