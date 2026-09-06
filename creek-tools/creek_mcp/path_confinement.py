"""Shared vault-root path confinement for the MCP tool surface.

MCP tools accept an *input_path* string from an untrusted caller. To stop
the surface from reading or ingesting arbitrary disk content, every tool
that touches a caller-supplied path routes it through
:func:`resolve_within_vault`, which confines the path to the vault root.

The confinement contract:

* An absolute path is accepted only if it resolves *inside* the vault.
* A relative path is joined under the resolved vault root (never the
  process cwd) before resolution.
* Resolution collapses ``..`` segments and follows symlinks, so neither a
  ``..`` traversal nor an in-vault symlink pointing outside can escape.
* Paths that resolve outside the vault return ``None`` for the caller to
  turn into a structured refusal.
* Paths the operating system refuses to resolve at all -- an embedded NUL
  byte, an unusable name -- take that same exit rather than raising out of
  the MCP boundary (#1089). "Provably inside the vault" is the only thing
  that earns a path back.

Existence is intentionally *not* checked here (``resolve(strict=False)``
semantics) so callers can distinguish an outside-the-vault refusal from a
not-found refusal with their own, clearer messages.
"""

from __future__ import annotations

from pathlib import Path


def resolve_within_vault(vault_path: Path, input_path: str) -> Path | None:
    """Return *input_path* resolved inside *vault_path*, or ``None`` if outside.

    Accepts either an absolute path inside the vault or a vault-relative
    path. Uses ``Path.resolve(strict=False)`` so non-existent staging
    directories still validate; existence is checked separately by the
    caller so it can emit a clean ``not found`` message instead of a
    silent ``None`` collapse.

    Args:
        vault_path: Vault root the path must resolve inside of.
        input_path: Caller-supplied path; absolute or relative to the
            vault root.

    Returns:
        The resolved absolute path when it lies inside the vault, or
        ``None`` when it escapes (after collapsing ``..`` and following
        symlinks), or ``None`` when it cannot be resolved at all.
    """
    vault_resolved = vault_path.resolve()
    candidate = Path(input_path)
    if not candidate.is_absolute():
        candidate = vault_resolved / candidate
    try:
        resolved = candidate.resolve()
    except (ValueError, OSError):
        # A path the operating system will not resolve is not a path inside
        # the vault, so it takes the same exit as one that escapes (#1089).
        #
        # `resolve()` reaches the filesystem, and a caller-supplied string can
        # make that call raise rather than answer. The reachable case is an
        # embedded NUL byte: `ValueError: lstat: embedded null character in
        # path`. It used to travel straight out of the MCP boundary as an
        # unhandled exception, because the `try` below covers only
        # `relative_to`.
        #
        # `OSError` is caught alongside it defensively, and the honest note is
        # that no *input* is known to reach it here: measured on this platform,
        # `resolve(strict=False)` swallows ENAMETOOLONG for both an over-long
        # component and a deeply nested path, answering instead of raising. It
        # is caught because `resolve()` is documented to touch the filesystem
        # and `strict=False` narrows which errors it suppresses rather than
        # promising none -- an unresolvable path must refuse, not escape,
        # whatever the errno. `tests/test_path_confinement.py` proves that arm
        # by making `resolve` raise, not by pretending a string reaches it.
        #
        # Refusing is the conservative direction: this function's contract is
        # "return a path only when it is provably inside the vault", and an
        # unresolvable path is not provably anything. Callers already turn
        # `None` into a structured refusal, so nothing downstream needs to
        # learn a new case.
        return None
    try:
        resolved.relative_to(vault_resolved)
    except ValueError:
        return None
    return resolved
