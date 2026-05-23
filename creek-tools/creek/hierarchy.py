"""Level-policy helpers for hierarchy-aware generators (FEAT-025).

The classify pipeline (FEAT-020..023) produces fragments at multiple
structural levels for the same underlying material — sentences carved
from paragraphs, paragraphs carved from sections, exchanges stitched
from messages, sessions stitched from bursts. Generators that summarise
or aggregate across the vault now need to pick a level: do they count
every fragment, only the most-atomic leaves, or only whole-source
documents (so sentence-level wavelength readings don't drown the signal)?

This module is the single source of truth for that choice. Each public
helper takes a list of :class:`~creek.models.Fragment` and a
:data:`LevelPolicy` and returns the filtered subset — no I/O, no
implicit vault scans, so callers can stay testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from creek.models import Fragment

LevelPolicy = Literal["leaves", "documents", "all"]
"""Which structural level a generator operates on.

- ``"leaves"``: keep only the most-atomic available representation
  *within the input set*. A fragment is a leaf when none of its
  ``child_ids`` appear among the inputs; this lets callers pass a
  partial slice and still get the right answer without re-scanning
  the vault.
- ``"documents"``: keep fragments at ``document`` / ``session`` level —
  the granularity at which whole-source phase reads make sense.
- ``"all"``: pre-FEAT-025 passthrough — used for regression tests and
  by callers that have already filtered upstream.
"""

_DOCUMENT_LEVELS: frozenset[str] = frozenset({"document", "session"})
"""Structural levels treated as "whole-source"-grained.

``document`` is the default for any flat ingestion; ``session`` is the
coarsest level the FEAT-022 zoom-out aggregator produces. Anything
finer (sentence, paragraph, subsection, section, exchange, burst) is
explicitly *not* a document for wavelength-read purposes.
"""


def select_by_policy(
    fragments: list[Fragment],
    policy: LevelPolicy,
) -> list[Fragment]:
    """Return the subset of *fragments* selected by *policy*.

    Args:
        fragments: Input fragments (any order, any hierarchy mix).
        policy: One of ``"leaves"``, ``"documents"``, ``"all"``.

    Returns:
        Fragments selected by *policy*, in input order.

    Raises:
        ValueError: If *policy* is not one of the documented values —
            a silent passthrough on a typo would mask the bug.
    """
    if policy not in get_args(LevelPolicy):
        msg = (
            f"Unknown level_policy {policy!r}; "
            f"expected one of {', '.join(get_args(LevelPolicy))}."
        )
        raise ValueError(msg)
    if policy == "all":
        return fragments.copy()
    if policy == "documents":
        return [f for f in fragments if str(f.level) in _DOCUMENT_LEVELS]
    ids_in_set = {f.id for f in fragments}
    return [
        f
        for f in fragments
        if not any(child_id in ids_in_set for child_id in f.child_ids)
    ]


def source_levels(fragments: list[Fragment]) -> list[str]:
    """Return the sorted distinct levels present in *fragments*.

    Used by :mod:`creek.compile.engine` to record which structural
    levels contributed to a compiled page in its frontmatter, so a
    future re-compile can detect drift.
    """
    return sorted({str(f.level) for f in fragments})


def structural_path_context(
    leaf: Fragment,
    by_id: dict[str, Fragment],
) -> list[str]:
    """Return the structural-path breadcrumb for *leaf*.

    Prefers the leaf's own ``structural_path`` (FEAT-020 round-trips it
    through frontmatter). Falls back to walking ``parent_id`` through
    *by_id* and collecting parent titles — useful when the persisted
    field is empty because the writer that produced this fragment
    pre-dated FEAT-020.

    Args:
        leaf: The fragment whose ancestry to render.
        by_id: Mapping of fragment ID → :class:`Fragment` for every
            fragment available in the current context. Walks stop at
            the first missing parent rather than raising.

    Returns:
        Breadcrumb from root to immediate parent, in display order.
        Empty when *leaf* has no ancestry to surface.
    """
    if leaf.structural_path:
        return leaf.structural_path.copy()
    path: list[str] = []
    current = leaf
    while current.parent_id is not None and current.parent_id in by_id:
        parent = by_id[current.parent_id]
        path.append(parent.title or parent.id)
        current = parent
    path.reverse()
    return path


__all__ = [
    "LevelPolicy",
    "select_by_policy",
    "source_levels",
    "structural_path_context",
]
