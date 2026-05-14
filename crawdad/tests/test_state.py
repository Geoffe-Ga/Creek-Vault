"""Tests for ``crawdad.state.load_session_state``."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawdad.state import (
    SessionState,
    StateUnavailableError,
    load_session_state,
)


def test_load_session_state_parses_full_report(vault_with_state: Path) -> None:
    """The four FEAT-013 sections land on the structured model."""
    state = load_session_state(vault_with_state)

    assert isinstance(state, SessionState)
    assert state.wavelength_snapshot is not None
    assert "rising" in state.wavelength_snapshot
    assert state.eddies == (
        "eddy-clarity",
        "eddy-emergence",
        "eddy-substrate",
    )
    assert state.threads == ("thread-voice-fidelity", "thread-paradox-hold")
    assert state.suggested_questions == (
        "What is surfacing in your liminal folder this week?",
        "Which eddy wants a draft?",
    )


def test_load_session_state_preserves_raw_body(vault_with_state: Path) -> None:
    """The raw ``latest.md`` bytes ride along for downstream prompts."""
    state = load_session_state(vault_with_state)

    assert "## Wavelength snapshot" in state.raw_markdown
    assert "## Suggested questions" in state.raw_markdown


def test_load_session_state_missing_file_raises(empty_vault: Path) -> None:
    """Missing ``latest.md`` raises a typed error the bot can handle."""
    with pytest.raises(StateUnavailableError) as excinfo:
        load_session_state(empty_vault)

    assert "creek state" in str(excinfo.value)


def test_load_session_state_partial_report(tmp_path: Path) -> None:
    """A report missing optional sections still parses cleanly."""
    state_dir = tmp_path / "00-Creek-Meta" / "State"
    state_dir.mkdir(parents=True)
    (state_dir / "latest.md").write_text(
        "# Creek state — empty week\n\n## Wavelength snapshot\n- (no data)\n",
        encoding="utf-8",
    )

    state = load_session_state(tmp_path)

    assert state.eddies == ()
    assert state.threads == ()
    assert state.suggested_questions == ()
    assert state.wavelength_snapshot is not None
    assert "(no data)" in state.wavelength_snapshot
