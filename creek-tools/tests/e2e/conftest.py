"""Shared fixtures for the end-to-end test suite.

Provides a synthetic vault + synthetic source-dir factory so each test
gets isolated, on-disk state without re-implementing directory layout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_VAULT_DIRS = (
    "00-Creek-Meta/Processing-Log",
    "00-Creek-Meta/Indexes",
    "00-Creek-Meta/Audit",
    "00-Creek-Meta/Consent",
    # 01-Fragments subfolders mirror creek/vault/writer.py's
    # _PLATFORM_SUBFOLDER mapping. Keep this in sync when adding a new
    # SourcePlatform value.
    "01-Fragments/Conversations",
    "01-Fragments/Messages",
    "01-Fragments/Writing",
    "01-Fragments/Writing/Substack",
    "01-Fragments/Journal",
    "01-Fragments/Technical",
    "01-Fragments/Notes",
    "01-Fragments/Documents",
    "01-Fragments/Data",
    "01-Fragments/Decks",
    "01-Fragments/Images",
    "01-Fragments/Unsorted",
    "02-Threads/Active",
    "02-Threads/Dormant",
    "02-Threads/Resolved",
    "03-Eddies",
    "04-Praxis/Daily",
    "04-Praxis/Seasonal",
    "04-Praxis/Situational",
    "05-Voice/Profiles",
    "05-Voice/Exemplars",
    "05-Voice/Patterns",
    "06-Wavelength/Reports",
    "07-Review",
    "08-Decisions/Active",
    "08-Decisions/Archive",
    "08-Decisions/Frameworks",
)


@pytest.fixture()
def synthetic_vault(tmp_path: Path) -> Path:
    """Create the canonical empty Creek vault directory tree."""
    for d in _VAULT_DIRS:
        (tmp_path / "vault" / d).mkdir(parents=True, exist_ok=True)
    return tmp_path / "vault"


@pytest.fixture()
def synthetic_source(tmp_path: Path) -> Path:
    """Empty source directory the test can populate before invoking the pipeline.

    pytest's ``tmp_path`` owns cleanup, so this fixture has no teardown
    code. The plain ``Path`` return type matches that — there is no
    yield-then-cleanup pattern hiding behind an ``Iterator`` annotation.
    """
    source = tmp_path / "source"
    source.mkdir()
    return source
