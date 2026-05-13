"""Shared fixtures for the CrawDad test suite."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def vault_with_state(tmp_path: Path) -> Path:
    """Return a vault root containing a populated ``State/latest.md``."""
    state_dir = tmp_path / "00-Creek-Meta" / "State"
    state_dir.mkdir(parents=True)
    (state_dir / "latest.md").write_text(
        dedent(
            """\
            # Creek state — 2026-W19

            ## Wavelength snapshot
            - Phase: **rising** (confidence 0.84)
            - Mode: **medicine**
            - Fragments observed: 42 (last 28 days)
            - Medicine share: 71.4% | Toxic share: 14.3%

            ## Active eddies
            - eddy-clarity
            - eddy-emergence
            - eddy-substrate

            ## Active threads
            - thread-voice-fidelity
            - thread-paradox-hold

            ## Suggested questions
            - What is surfacing in your liminal folder this week?
            - Which eddy wants a draft?
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """Return a vault root that has no ``State/latest.md``."""
    (tmp_path / "00-Creek-Meta").mkdir()
    return tmp_path
