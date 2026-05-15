"""Tests for ``crawdad.skill_loader``."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawdad.skill_loader import (
    VoiceSkillStack,
    load_skills_for_session,
)
from crawdad.state import SessionState


def _make_state(phase_snippet: str) -> SessionState:
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot=f"Phase: **{phase_snippet}** (confidence 0.84)",
        eddies=(),
        threads=(),
        suggested_questions=(),
    )


@pytest.fixture
def vault_with_voice_tree(tmp_path: Path) -> Path:
    """Create a fully-populated voice-skill tree under ``creek-skills/``."""
    skills = tmp_path / "creek-skills"
    (skills / "voice-core").mkdir(parents=True)
    (skills / "voice-core" / "SKILL.md").write_text(
        "# Voice core\nThe load-bearing voice rules.", encoding="utf-8"
    )
    (skills / "phases").mkdir()
    (skills / "phases" / "rising.SKILL.md").write_text(
        "# Rising\nAct on energy; mine and draft are welcome.", encoding="utf-8"
    )
    (skills / "phases" / "bottoming-out.SKILL.md").write_text(
        "# Bottoming Out\nCompost; never urge action.", encoding="utf-8"
    )
    (skills / "registers").mkdir()
    (skills / "registers" / "confessional.SKILL.md").write_text(
        "# Confessional\nReflective, first-person, soft.", encoding="utf-8"
    )
    (skills / "registers" / "praxis.SKILL.md").write_text(
        "# Praxis\nActionable, second-person.", encoding="utf-8"
    )
    return tmp_path


def test_load_skills_returns_voice_core_phase_and_register(
    vault_with_voice_tree: Path,
) -> None:
    """Default session loads voice-core + matching phase + confessional."""
    state = _make_state("rising")

    stack = load_skills_for_session(vault_path=vault_with_voice_tree, state=state)

    assert isinstance(stack, VoiceSkillStack)
    bodies = stack.bodies()
    assert any("Voice core" in body for body in bodies)
    assert any("Rising" in body for body in bodies)
    assert any("Confessional" in body for body in bodies)


def test_load_skills_picks_phase_matching_wavelength(
    vault_with_voice_tree: Path,
) -> None:
    """A Bottoming-Out wavelength loads ``bottoming-out.SKILL.md``."""
    state = _make_state("bottoming-out")

    stack = load_skills_for_session(vault_path=vault_with_voice_tree, state=state)

    bodies = stack.bodies()
    assert any("Bottoming Out" in body for body in bodies)
    assert not any("Rising" in body for body in bodies)


def test_load_skills_supports_extra_registers(
    vault_with_voice_tree: Path,
) -> None:
    """Caller can request additional registers beyond the default."""
    state = _make_state("rising")

    stack = load_skills_for_session(
        vault_path=vault_with_voice_tree, state=state, extra_registers=("praxis",)
    )

    bodies = stack.bodies()
    assert any("Praxis" in body for body in bodies)


def test_load_skills_missing_tree_returns_empty_stack(tmp_path: Path) -> None:
    """A vault without ``creek-skills/`` yields an empty stack (no crash)."""
    state = _make_state("rising")

    stack = load_skills_for_session(vault_path=tmp_path, state=state)

    assert stack.bodies() == []


def test_load_skills_missing_phase_file_skips_it(tmp_path: Path) -> None:
    """When the phase file is absent the stack still loads voice-core."""
    skills = tmp_path / "creek-skills"
    (skills / "voice-core").mkdir(parents=True)
    (skills / "voice-core" / "SKILL.md").write_text(
        "# Voice core only", encoding="utf-8"
    )
    state = _make_state("rising")

    stack = load_skills_for_session(vault_path=tmp_path, state=state)

    bodies = stack.bodies()
    assert len(bodies) == 1
    assert "Voice core only" in bodies[0]


def test_load_skills_handles_state_without_wavelength(
    vault_with_voice_tree: Path,
) -> None:
    """``state=None`` (or no wavelength) still loads voice-core + default register."""
    stack = load_skills_for_session(vault_path=vault_with_voice_tree, state=None)

    bodies = stack.bodies()
    assert any("Voice core" in body for body in bodies)
    assert any("Confessional" in body for body in bodies)


def test_load_skills_refuses_unsafe_register_name(
    vault_with_voice_tree: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Register names with path-separator / traversal chars are refused.

    FEAT-016 will let slash commands set extra registers from user
    input; without this guard a name like ``../../etc/passwd`` would
    escape ``creek-skills/registers/``.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="crawdad.skill_loader")

    stack = load_skills_for_session(
        vault_path=vault_with_voice_tree,
        state=_make_state("rising"),
        extra_registers=("../../etc/passwd", "Praxis"),  # uppercase too
    )

    names = stack.names()
    # voice-core + phase:rising + register:confessional only.
    # Both invalid register names ("../..." and "Praxis") are refused.
    assert "register:../../etc/passwd" not in names
    assert "register:Praxis" not in names
    assert any("must match" in record.message for record in caplog.records)


def test_voice_skill_stack_as_prompt_context(vault_with_voice_tree: Path) -> None:
    """``as_prompt_context`` concatenates with separators for the composer prompt."""
    state = _make_state("rising")

    stack = load_skills_for_session(vault_path=vault_with_voice_tree, state=state)
    context = stack.as_prompt_context()

    assert "Voice core" in context
    assert "Rising" in context
    assert "Confessional" in context
    # Skill files are separated so the LLM can distinguish them.
    assert "---" in context or context.count("\n\n") >= 2
