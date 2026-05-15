"""Voice-skill tree loader (FEAT-015).

CrawDad reads ``<vault>/creek-skills/`` at session start to assemble
the skill stack the Sonnet composer conditions on. Layout the loader
expects (any subset may be present — missing files are skipped, not an
error):

::

    <vault>/creek-skills/
    ├── voice-core/SKILL.md           # always loaded
    ├── phases/<phase>.SKILL.md       # matched against session wavelength
    └── registers/<register>.SKILL.md # ``confessional`` by default

The session-state wavelength snapshot supplies the phase. The default
register is :data:`crawdad.config.DEFAULT_REGISTER` ("confessional",
the long-term-memory reflective register from the ontology). Callers
can ask for extra registers via ``extra_registers=...`` — FEAT-016's
slash commands will use this to switch into ``praxis`` for actionable
turns.

The loader is forgiving: a vault without a fleshed-out voice tree
still produces a :class:`VoiceSkillStack`, just an empty one, and the
composer falls back to its built-in voice-tolerant prompt.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from crawdad.config import CREEK_SKILLS_DIRNAME, DEFAULT_REGISTER

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.skill_loader")

_PHASE_RE = re.compile(r"phase[:\s]*\*{0,2}([a-z\-]+)", re.IGNORECASE)
# Same shape as the captured phase slug. Used to validate caller-supplied
# register names so path construction can't be tricked into traversal.
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z\-]*$")
_PROMPT_SEPARATOR = "\n\n---\n\n"


class VoiceSkill(BaseModel):
    """One loaded skill file."""

    model_config = ConfigDict(frozen=True)

    name: str
    body: str


class VoiceSkillStack(BaseModel):
    """The ordered set of skill files the composer conditions on.

    Order matters: ``voice-core`` first (the load-bearing rules), then
    the phase skill (wavelength context), then registers. The composer
    pastes them in this order into its prompt.
    """

    model_config = ConfigDict(frozen=True)

    skills: tuple[VoiceSkill, ...]

    def bodies(self) -> list[str]:
        """Return the raw markdown body of each skill, in order."""
        return [skill.body for skill in self.skills]

    def names(self) -> list[str]:
        """Return the human-readable skill names (for logs / debugging)."""
        return [skill.name for skill in self.skills]

    def as_prompt_context(self) -> str:
        """Concatenate all skill bodies for embedding in the composer prompt."""
        return _PROMPT_SEPARATOR.join(self.bodies())


def load_skills_for_session(
    *,
    vault_path: Path,
    state: SessionState | None,
    extra_registers: Iterable[str] = (),
) -> VoiceSkillStack:
    """Read the voice-skill files matching this session's context.

    Args:
        vault_path: Root of the user's Obsidian vault. The loader looks
            for ``<vault_path>/creek-skills/`` and silently returns an
            empty stack if the directory is missing.
        state: Session-state snapshot for phase resolution. ``None``
            (or a state with no wavelength) skips the phase skill.
        extra_registers: Register names to load in addition to
            :data:`crawdad.config.DEFAULT_REGISTER`. Unknown registers
            are skipped with a DEBUG log.

    Returns:
        A :class:`VoiceSkillStack` whose order is voice-core →
        phase → default register → extra registers.
    """
    root = vault_path / CREEK_SKILLS_DIRNAME
    if not root.is_dir():
        _LOGGER.info(
            "no voice-skill tree at %s; composer will run without skill context",
            root,
        )
        return VoiceSkillStack(skills=())

    collected: list[VoiceSkill] = []
    voice_core = root / "voice-core" / "SKILL.md"
    _append_if_present(collected, voice_core, name="voice-core")

    phase = _phase_from_state(state)
    if phase:
        phase_path = root / "phases" / f"{phase}.SKILL.md"
        _append_if_present(collected, phase_path, name=f"phase:{phase}")

    requested_registers = [DEFAULT_REGISTER, *extra_registers]
    seen: set[str] = set()
    for register in requested_registers:
        if register in seen:
            continue
        seen.add(register)
        if not _SAFE_NAME_RE.match(register):
            # FEAT-016 will let slash commands set extra registers from
            # user input; refuse any name that would escape the registers/
            # subdirectory (path traversal, absolute paths, dotfiles).
            _LOGGER.warning(
                "ignoring register name %r — must match [a-z][a-z-]*", register
            )
            continue
        register_path = root / "registers" / f"{register}.SKILL.md"
        _append_if_present(collected, register_path, name=f"register:{register}")

    return VoiceSkillStack(skills=tuple(collected))


def _append_if_present(target: list[VoiceSkill], path: Path, *, name: str) -> None:
    """Read *path* and append a :class:`VoiceSkill`, or log+skip on absence."""
    if not path.is_file():
        _LOGGER.debug("skill %s not present at %s; skipping", name, path)
        return
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        _LOGGER.debug("skill %s at %s is empty; skipping", name, path)
        return
    target.append(VoiceSkill(name=name, body=body))


def _phase_from_state(state: SessionState | None) -> str | None:
    """Extract a normalised phase slug from the session state wavelength."""
    if state is None or not state.wavelength_snapshot:
        return None
    match = _PHASE_RE.search(state.wavelength_snapshot)
    if not match:
        return None
    return match.group(1).lower()
