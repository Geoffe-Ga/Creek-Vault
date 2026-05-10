"""Destination router for ``creek save`` targets.

Each :class:`SaveTarget` maps to a fixed vault subdirectory. The
``paradox`` target is special: it *always* lands in
``10-Liminal/Paradoxes/`` regardless of any other input (FEAT-009
acceptance criterion). The router is a pure function so the writer,
the CLI, and tests all share the same source of truth.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

INTIMATE_STUB_RELPATH = Path("10-Liminal/Compost/intimate-stubs")
"""Where intimate-tier bodies land — gitignored at the repo level."""


class SaveTarget(StrEnum):
    """The six target types ``creek save --target`` accepts."""

    THREAD = "thread"
    EDDY = "eddy"
    PRAXIS = "praxis"
    PARADOX = "paradox"
    UNNAMED = "unnamed"
    DRAFT = "draft"


_TARGET_SUBDIRS: dict[SaveTarget, tuple[str, ...]] = {
    SaveTarget.THREAD: ("02-Threads", "Active"),
    SaveTarget.EDDY: ("03-Eddies",),
    SaveTarget.PRAXIS: ("04-Praxis", "Situational"),
    SaveTarget.PARADOX: ("10-Liminal", "Paradoxes"),
    SaveTarget.UNNAMED: ("10-Liminal", "Unnamed"),
    SaveTarget.DRAFT: ("07-Voice", "Drafts"),
}


def target_directory(vault_path: Path, target: SaveTarget) -> Path:
    """Return the vault subdirectory for *target* under *vault_path*."""
    return vault_path.joinpath(*_TARGET_SUBDIRS[target])
