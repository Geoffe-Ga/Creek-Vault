"""One cross-process boundary for vault content mutations (#1799).

Journal upsert and withdrawal, document upload, classification, linking, and
compilation all load state before writing it.  If those paths use different
locks, a writer that loaded plaintext before withdrawal can commit it after
DELETE has already reported success.  Every such operation therefore holds the
lock returned by :func:`content_mutation_lock_path` across its complete
load-to-write window.

Two participants hold it for a gate rather than a write.  ``creek.upload``
holds it from its #970 overwrite gate through the ingest that gate admits
(#2069), and the per-entry classification read ``creek.classify.entry`` holds
it while it takes the one snapshot it both gates on and answers from (#2068):
each reads a privacy tier that classification rewrites under this lock, and
a tier read outside it can be stale by the time it is acted on.

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
