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
        symlinks).
    """
    vault_resolved = vault_path.resolve()
    candidate = Path(input_path)
    if not candidate.is_absolute():
        candidate = vault_resolved / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(vault_resolved)
    except ValueError:
        return None
    return resolved
