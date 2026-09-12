"""One cross-process boundary for vault content mutations (#1799).

Journal upsert and withdrawal, classification, linking, and compilation all
load state before writing it.  If those paths use different locks, a writer
that loaded plaintext before withdrawal can commit it after DELETE has already
reported success.  Every such operation therefore holds the lock returned by
:func:`content_mutation_lock_path` across its complete load-to-write window.

The filename is deliberately content-wide rather than journal-specific.  The
empty lock file contains no vault-derived data, and whole-vault erasure keeps
its inode so deleting a held pathname cannot split the critical section.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

CONTENT_MUTATION_LOCK_RELPATH: Final[Path] = Path(
    "00-Creek-Meta/locks/content-mutations.lock"
)
"""Relative path of the shared vault-content mutation lock."""

CONTENT_MUTATION_BUSY_REASON: Final[str] = "vault content mutation busy"
"""Content-free remote refusal for a bounded mutation-lock timeout."""


def content_mutation_lock_path(vault_path: Path) -> Path:
    """Return the lock serialising load-to-write content operations.

    Args:
        vault_path: Root of the vault whose content may be mutated.

    Returns:
        The canonical cross-process lock path inside that vault.
    """
    return vault_path / CONTENT_MUTATION_LOCK_RELPATH
