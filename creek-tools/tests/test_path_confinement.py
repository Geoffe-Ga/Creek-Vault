"""Unit tests for the shared vault-root path-confinement helper (issue #819).

``resolve_within_vault`` is the single source of truth guarding every MCP
tool that accepts a caller-supplied ``input_path`` (``creek.ingest``,
``creek.redact.scan``). These focused tests exercise the function directly
so a regression localizes here instead of surfacing only through a tool
wrapper's integration test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from creek_mcp.path_confinement import resolve_within_vault

if TYPE_CHECKING:
    import pytest

# ``Path`` is imported at runtime, not under TYPE_CHECKING:
# ``test_an_os_error_from_resolve_is_refused`` patches ``Path.resolve``
# itself, so the class object has to exist when the test runs (#1089).


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


def test_a_nul_byte_is_refused_rather_than_raised(tmp_path: Path) -> None:
    """An unresolvable path takes the refusal exit, not the exception one.

    Issue #1089. ``resolve()`` sits on the filesystem, and a caller-supplied
    string with an embedded NUL makes it raise ``ValueError: lstat: embedded
    null character in path``. That call used to sit *outside* the ``try``
    below it -- which covers only ``relative_to`` -- so the exception left the
    MCP boundary unhandled instead of becoming the ordinary refusal every
    caller already knows how to render.
    """
    assert resolve_within_vault(tmp_path, "staging/no\x00pe.md") is None


def test_a_nul_byte_in_an_absolute_path_is_refused_too(tmp_path: Path) -> None:
    """Both arms reach the same ``resolve()``, so both must be guarded.

    The relative arm joins under the vault root first; the absolute arm does
    not. A guard placed on only one of them would leave the other raising.
    """
    assert resolve_within_vault(tmp_path, f"{tmp_path}/no\x00pe.md") is None


def test_an_os_error_from_resolve_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``OSError`` arm is proven by injection, not by a pretend input.

    No string is known to reach it: measured on this platform,
    ``resolve(strict=False)`` swallows ENAMETOOLONG for an over-long component
    and for a deeply nested path alike, answering rather than raising. Rather
    than ship a test whose fixture quietly stops triggering the branch -- a
    test that passes while proving nothing -- this makes ``resolve`` raise
    directly. The branch exists because ``strict=False`` narrows which errors
    ``resolve`` suppresses rather than promising none.
    """

    real_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        """Resolve normally unless the path is the injected failure target.

        Args:
            self: The path being resolved.
            *args: Passed through to the real ``Path.resolve``.
            **kwargs: Passed through to the real ``Path.resolve``.

        Returns:
            The genuinely resolved path.

        Raises:
            OSError: When the path carries the failure marker.
        """
        # Only the CANDIDATE fails. Patching `Path.resolve` wholesale also
        # breaks `vault_path.resolve()` on the function's first line, which is
        # deliberately unguarded: the vault root comes from operator config,
        # not from the caller, and a vault that cannot be resolved is a
        # misconfiguration that must stay loud rather than silently becoming
        # "everything is outside the vault". The first draft of this test
        # patched everything and failed on exactly that call -- which is the
        # evidence that line is still unguarded, and should be.
        if "boom" in str(self):
            message = "simulated filesystem failure"
            raise OSError(message)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert resolve_within_vault(tmp_path, "staging/boom.md") is None


def test_a_resolvable_path_is_unaffected_by_the_guard(tmp_path: Path) -> None:
    """The guard must not swallow the ordinary success path.

    A ``try`` placed too widely would turn a legitimate in-vault path into a
    refusal, which fails closed but breaks every tool. This is the companion
    that stops the fix from being "return None always".
    """
    target = tmp_path / "staging" / "note.md"
    assert resolve_within_vault(tmp_path, str(target)) == target.resolve()
