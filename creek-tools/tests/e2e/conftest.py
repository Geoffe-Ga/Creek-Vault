"""Shared fixtures for the end-to-end test suite.

Provides a synthetic vault + synthetic source-dir factory so each test
gets isolated, on-disk state without re-implementing directory layout.

The vault is built by the *real* scaffold — the same code path
``creek init`` runs — rather than a hand-written directory list. The
list this replaced (issue #1025) had drifted into fiction: it created
``05-Voice/``, ``06-Wavelength/`` and ``07-Review/``, none of which any
production module has ever written to, while omitting most of the tree
that production does write to. Nothing ever failed, because every writer
calls ``mkdir(parents=True, exist_ok=True)`` and quietly conjured
whatever the fixture had forgotten. Deriving the fixture from
:func:`creek.scaffold.scaffold_vault` means an e2e test now runs against
the vault a user actually gets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.scaffold import scaffold_vault

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def synthetic_vault(tmp_path: Path) -> Path:
    """Create a canonical Creek vault via the ``creek init`` code path."""
    vault = tmp_path / "vault"
    scaffold_vault(vault)
    return vault


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
