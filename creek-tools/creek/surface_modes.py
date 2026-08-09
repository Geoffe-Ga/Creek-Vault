"""The value-dispatch vocabularies every Creek frontend must agree on.

Two commands dispatch on the *value* of a flag rather than on its presence —
``creek link --method`` and ``creek report --type`` — and each is exposed
twice: once by :mod:`creek.cli` and once by a tool wrapper in
:mod:`creek_mcp.tools`. Until #1252/#1253 each frontend carried its own
retyped copy of the accepted values, and both copies had silently drifted:
``creek_mcp.tools.link`` had lost ``"threads"`` (the whole thread half of
#880, unreachable over MCP), and ``creek_mcp.tools.report`` advertised six of
eleven types.

Neither bug was a typo; both were the *mechanism*. A hand-maintained second
copy fails open — a new CLI mode simply never appears on the other surface, no
error is raised at import or at call time, and the only symptom is a caller
being told the mode does not exist. So the copies are gone: this module is the
one declaration, and every frontend reads it.

Adding a mode means adding it **here**. Both frontends then accept it
immediately, which is the point — and the wiring contract
(``tests/test_wiring_contract.py``) compares what each surface *advertises to a
caller* rather than the constant it imports, so a re-introduced copy is caught
by behaviour rather than by inspection.

Being reachable is not the same as being *served*: a mode this module names
that a frontend genuinely cannot honour must be refused **by name and with a
stated reason** (see ``creek_mcp.tools.report``'s tier-blind refusal). Dropping
it from the advertised set is what produced #1253 in the first place.

This module deliberately imports nothing. It is read at ``creek.cli`` import
time, which is on the critical path of every CLI invocation, and it is read by
MCP tool wrappers that must not drag a frontend's dependencies in behind it.
"""

from __future__ import annotations

from typing import Final

LINK_METHODS: Final[tuple[str, ...]] = (
    "embeddings",
    "temporal",
    "eddies",
    "threads",
)
"""Linker stages ``creek link`` / ``creek.link`` accept, in help-text order.

Mirrors the dispatch inside :func:`creek.link.link_engine.run_link`, which is
where each name is turned into a linker.
"""

REPORT_TYPES: Final[tuple[str, ...]] = (
    "tags",
    "unnamed",
    "voice",
    "fingerprint",
    "decisions",
    "lexicon",
    "rhetorical-patterns",
    "mode-profiles",
    "paradox",
    "synchronicity",
    "wavelength",
)
"""Report types ``creek report`` / ``creek.report`` accept, in dispatch order.

Ten of these are keys of :data:`creek.cli._REPORT_DISPATCH`; ``wavelength`` is
the eleventh and is special-cased by ``creek report`` because it needs
``--period``, so enumerating that dict alone under-covers the surface by one —
the trap #1027 documented. ``tests/test_wiring_contract.py``'s
``test_report_error_message_lists_exactly_the_declared_types`` pins this tuple
against the dispatch dict so the two cannot drift apart either.
"""
