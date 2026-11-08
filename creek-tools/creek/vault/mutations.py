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

Ingest joins the boundary at its **surfaces** (#2079): ``creek ingest`` and the
``creek sync`` Tier-A ingest (``creek.cli._run_ingest``), the ``creek.ingest``
MCP tool, and the ingest phase of a Drive sync, each of which writes
``01-Fragments``, its ledger and any tombs while classify or link could be
reading them.  They take the lock through :func:`hold_content_mutation`, never
inside :func:`creek.ingest.pipeline.run_ingest`: the lock is not reentrant, and
``creek.upload`` and ``creek.journal`` already call ``run_ingest`` while holding
it.  ``run_ingest`` and :class:`~creek.vault.writer.VaultWriter` therefore stay
lock-free, and whoever calls them owns the boundary.  A Drive sync nests this
lock inside its own ``gdrive.lock`` and around the ingest only, never around
the network download; no holder of this lock ever takes ``gdrive.lock``, so
the order is always connector first, content second.

The classify, link and compile engines take the lock through
:func:`hold_content_mutation` as well (#2087), so losing the acquisition race
on those paths is :class:`ContentMutationBusyError` and their CLI and sync
callers can tell it from a timeout raised mid-pass.  Manual review
(``creek.classify.review_runner``) keeps its raw acquisition on purpose: its
save path records the timeout as an ``OSError`` and lets the session continue.

The filename is deliberately content-wide rather than journal-specific.  The
empty lock file contains no vault-derived data, and whole-vault erasure keeps
its inode so deleting a held pathname cannot split the critical section.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek._fslock import VaultLockTimeoutError, vault_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

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


class ContentMutationBusyError(Exception):
    """Another writer held the content-mutation lock past the bounded wait.

    Raised only when *acquiring* the lock times out, so the guarded body never
    started and nothing was written.  Deliberately not a
    :class:`~creek._fslock.VaultLockTimeoutError`: a timeout raised *under* the
    hold (the writer's ``.id-index.lock``, say) can follow writes that already
    landed, and must keep propagating as itself rather than be reported as a
    vault nothing touched.  Keeping the two types disjoint also means no
    existing ``except VaultLockTimeoutError`` arm can swallow this one.
    """


@contextmanager
def hold_content_mutation(vault_path: Path) -> Iterator[None]:
    """Hold *vault_path*'s content-mutation lock for the block (#2079).

    Acquisition-only translation: a timeout while taking the lock becomes
    :class:`ContentMutationBusyError`; every exception the body raises,
    including a :class:`~creek._fslock.VaultLockTimeoutError` from a lock taken
    inside it, propagates unchanged.  The wait is the primitive's default
    (:data:`creek._fslock.DEFAULT_LOCK_TIMEOUT_SECONDS`), which is pinned below
    the ``/v1`` request deadline in one place.

    Args:
        vault_path: Root of the vault whose content the block mutates.

    Yields:
        Nothing. The block runs with the lock held.

    Raises:
        ContentMutationBusyError: If the lock was still held elsewhere when the
            bounded wait expired.
    """
    with ExitStack() as hold:
        try:
            hold.enter_context(vault_lock(content_mutation_lock_path(vault_path)))
        except VaultLockTimeoutError as exc:
            raise ContentMutationBusyError(CONTENT_MUTATION_BUSY_REASON) from exc
        yield
