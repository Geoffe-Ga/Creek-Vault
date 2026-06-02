"""Destination router for ``creek save`` targets.

Each :class:`SaveTarget` maps to a fixed vault subdirectory. The
``paradox`` target is special: it *always* lands in
``10-Liminal/Paradoxes/`` regardless of any other input (FEAT-009
acceptance criterion). The router is a pure function so the writer,
the CLI, and tests all share the same source of truth.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from creek.save._constants import INTIMATE_STUB_RELPATH

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "INTIMATE_STUB_RELPATH",
    "TARGET_SUBDIRS",
    "SaveTarget",
    "target_directory",
]


class SaveTarget(StrEnum):
    """The target types ``creek save --target`` accepts.

    ``ai-as-user`` (FEAT-041 §7) is special: it files AI-authored output the
    owner chose to keep into ``11-Other-Authors/ai-as-user/`` as a real,
    AI-attributed fragment, so the Retrieval specialist can read it back
    without it ever counting as the owner's own voice.
    """

    THREAD = "thread"
    EDDY = "eddy"
    PRAXIS = "praxis"
    PARADOX = "paradox"
    UNNAMED = "unnamed"
    DRAFT = "draft"
    AI_AS_USER = "ai-as-user"


TARGET_SUBDIRS: dict[SaveTarget, tuple[str, ...]] = {
    SaveTarget.THREAD: ("02-Threads", "Active"),
    SaveTarget.EDDY: ("03-Eddies",),
    SaveTarget.PRAXIS: ("04-Praxis", "Situational"),
    SaveTarget.PARADOX: ("10-Liminal", "Paradoxes"),
    SaveTarget.UNNAMED: ("10-Liminal", "Unnamed"),
    SaveTarget.DRAFT: ("07-Voice", "Drafts"),
    SaveTarget.AI_AS_USER: ("11-Other-Authors", "ai-as-user"),
}
"""Canonical mapping of save targets to vault subdirectories.

Exposed publicly so tests and downstream callers (e.g. the MCP wrapper
in FEAT-016) can parametrize against the same data the router uses,
rather than re-declaring the mapping and risking drift. The underscore
prefix was dropped in PR #220 round 4 after a reviewer flagged the
private-symbol coupling in :mod:`tests.test_save`.
"""


def target_directory(vault_path: Path, target: SaveTarget) -> Path:
    """Return the vault subdirectory for *target* under *vault_path*."""
    return vault_path.joinpath(*TARGET_SUBDIRS[target])
