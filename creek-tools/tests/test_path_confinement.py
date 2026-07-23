"""Unit tests for the shared vault-root path-confinement helper (issue #819).

``resolve_within_vault`` is the single source of truth guarding every MCP
tool that accepts a caller-supplied ``input_path`` (``creek.ingest``,
``creek.redact.scan``). These focused tests exercise the function directly
so a regression localizes here instead of surfacing only through a tool
wrapper's integration test.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from creek_mcp.path_confinement import resolve_within_vault

if TYPE_CHECKING:
    from pathlib import Path


def test_absolute_path_inside_vault_is_accepted(tmp_path: Path) -> None:
    """An absolute path under the vault resolves to that same canonical path."""
    target = tmp_path / "staging" / "note.md"
    resolved = resolve_within_vault(tmp_path, str(target))
    assert resolved == target.resolve()


def test_relative_path_binds_to_vault_not_cwd(tmp_path: Path) -> None:
    """A relative path is joined under the vault root, never the process cwd."""
    resolved = resolve_within_vault(tmp_path, "staging/note.md")
    assert resolved == (tmp_path / "staging" / "note.md").resolve()


def test_vault_root_itself_is_accepted(tmp_path: Path) -> None:
    """The vault root resolves to itself and is inside the vault."""
    assert resolve_within_vault(tmp_path, str(tmp_path)) == tmp_path.resolve()


def test_absolute_path_outside_vault_is_rejected(tmp_path: Path) -> None:
    """An absolute path outside the vault returns ``None``."""
    assert resolve_within_vault(tmp_path, str(tmp_path.parent / "outside.md")) is None


def test_dot_dot_traversal_is_rejected(tmp_path: Path) -> None:
    """A ``..`` sequence that escapes the vault is collapsed then rejected."""
    assert resolve_within_vault(tmp_path, str(tmp_path / ".." / "escape.md")) is None


def test_symlink_escaping_vault_is_rejected(tmp_path: Path) -> None:
    """A symlink inside the vault pointing outside it is followed then rejected."""
    outside = tmp_path.parent / "secret.md"
    outside.write_text("# secret\n", encoding="utf-8")
    link = tmp_path / "link-to-secret.md"
    os.symlink(outside, link)
    assert resolve_within_vault(tmp_path, str(link)) is None


def test_symlink_staying_inside_vault_is_accepted(tmp_path: Path) -> None:
    """A symlink whose target is inside the vault resolves to that target."""
    inside = tmp_path / "real.md"
    inside.write_text("# real\n", encoding="utf-8")
    link = tmp_path / "link-to-real.md"
    os.symlink(inside, link)
    assert resolve_within_vault(tmp_path, str(link)) == inside.resolve()
