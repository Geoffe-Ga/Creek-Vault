"""Voice-skill discovery for the Writing Desk's voice agent (FEAT-041, #471).

The voice agent conditions the owner's voice on the ``creek-skills`` tree that
``creek skills`` writes under the vault (see
:mod:`creek.generate.skills`). This module locates the voice-core, phase, and
register ``SKILL.md`` files for a run and inlines them as prompt sections.

It is implemented natively in ``creek-tools`` — it never imports from the
``crawdad`` sibling. The native generator writes the voice-core to
``creek-skills/meta/voice-core.SKILL.md``; this loader also accepts the
crawdad-style ``creek-skills/voice-core/SKILL.md`` so a tree authored either
way resolves. Missing files are skipped silently so the desk runs on a
partially-built skill tree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Vault subdirectory holding the voice-skill tree (``creek skills`` output).
_SKILLS_SUBDIR: str = "creek-skills"

#: Subdirectories within the skill tree, mirroring :mod:`creek.generate.skills`.
_META_DIR: str = "meta"
_PHASES_DIR: str = "phases"
_REGISTERS_DIR: str = "registers"

_SKILL_SUFFIX: str = ".SKILL.md"

#: Candidate voice-core locations, in resolution order. The native generator
#: writes ``meta/voice-core.SKILL.md``; the first entry accepts the
#: crawdad-style ``voice-core/SKILL.md`` so either tree layout resolves.
_VOICE_CORE_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("voice-core", "SKILL.md"),
    (_META_DIR, f"voice-core{_SKILL_SUFFIX}"),
)


def _skills_root(vault: Path) -> Path:
    """Return the ``creek-skills`` tree root under *vault*."""
    return vault / _SKILLS_SUBDIR


def find_voice_core(vault: Path) -> Path | None:
    """Return the voice-core ``SKILL.md`` path for *vault*, or ``None``.

    Checks each :data:`_VOICE_CORE_CANDIDATES` location in order and returns
    the first file that exists.

    Args:
        vault: The vault root to search under.

    Returns:
        The voice-core skill path, or ``None`` when no candidate exists.
    """
    root = _skills_root(vault)
    for parts in _VOICE_CORE_CANDIDATES:
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return candidate
    return None


def find_phase_skill(vault: Path, phase: str | None) -> Path | None:
    """Return the ``phases/<phase>.SKILL.md`` path, or ``None``.

    Args:
        vault: The vault root to search under.
        phase: The dominant phase slug (e.g. ``"rising"``); ``None`` or
            ``"unclassified"`` resolves to no phase skill.

    Returns:
        The phase skill path when it exists, else ``None``.
    """
    if not phase or phase == "unclassified":
        return None
    candidate = _skills_root(vault) / _PHASES_DIR / f"{phase}{_SKILL_SUFFIX}"
    return candidate if candidate.is_file() else None


def find_register_skill(vault: Path, register: str | None) -> Path | None:
    """Return the ``registers/<register>.SKILL.md`` path, or ``None``.

    Args:
        vault: The vault root to search under.
        register: The voice register slug; ``None`` resolves to no skill.

    Returns:
        The register skill path when it exists, else ``None``.
    """
    if not register:
        return None
    candidate = _skills_root(vault) / _REGISTERS_DIR / f"{register}{_SKILL_SUFFIX}"
    return candidate if candidate.is_file() else None


def read_skill(path: Path) -> str:
    """Return the stripped text of skill *path*, or ``""`` when unreadable.

    Args:
        path: The ``SKILL.md`` file to read.

    Returns:
        The file's stripped contents, or an empty string on a read error so
        a missing/locked skill is skipped silently rather than crashing.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # A locked/unreadable file or a vault-authored SKILL.md with non-UTF-8
        # bytes must skip silently rather than crash the voice render (#501).
        logger.warning("Could not read voice-skill file %s; skipping.", path)
        return ""
