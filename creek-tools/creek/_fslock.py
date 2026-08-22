"""Advisory locking for one vault path, across threads *and* processes (#1590).

Creek's write paths used to guard themselves with per-instance
:class:`threading.Lock` objects alone. That is enough for two calls on
one object and nothing else, and ``/v1`` is not even one object: a sync
runs off the event loop in a worker thread
(``starlette.concurrency.run_in_threadpool``),
:func:`creek.ingest.pipeline.run_ingest` builds a fresh
:class:`creek.vault.writer.VaultWriter` on every call, a deployment may
run more than one uvicorn worker, and the operator's ``creek`` CLI is a
different process entirely. Two overlapping Drive syncs measurably
produced two files carrying **one** fragment id — one source unit, two
notes, two ledger rows.

Two callers hold locks from this module, and they are deliberately
different keys:

* ``creek_mcp.tools.drive`` takes ``gdrive.lock`` around one whole
  download-and-ingest window (#1590);
* :class:`creek.vault.writer.VaultWriter` takes
  ``<fragment dir>/.id-index.lock`` around each index check-then-act
  (#1603).

Keeping them disjoint is a correctness requirement, not tidiness:
:func:`vault_lock` is **not reentrant** — the thread lock in front of
``flock`` is a plain :class:`threading.Lock` — so a write lock named
after the ledger would nest inside the sync's own lock, burn the full
timeout, and refuse every sync.

:func:`vault_lock` is the missing primitive: a named lock file, an
exclusive ``fcntl`` advisory lock over it, and a per-resolved-path
:class:`threading.Lock` in front of that. Both halves are needed:

* the ``flock`` is what one *process* cannot see another take;
* the thread lock is what makes the guarantee hold where ``fcntl`` is
  absent (Windows), and removes any reliance on how a platform scopes
  ``flock`` between two descriptors opened by one process.

**What this does not promise.** ``fcntl`` is POSIX-only, so on Windows
this degrades to in-process serialisation — the same degradation
:class:`creek.audit.log.AuditLog` already ships. ``flock`` over a network
filesystem (NFS, SMB) is advisory at best and unimplemented at worst; a
mount that refuses the call is logged and the lock degrades rather than
failing the caller's work outright, because a vault that cannot be
locked is still a vault that must be writable.

**Why the wait is polled rather than blocking.** A blocking
``LOCK_EX`` inside a worker thread cannot be cancelled by the request
timeout above it, so a queued request would be answered ``503`` while
its thread stayed pinned to the lock indefinitely. Polling to a deadline
gives the caller a refusal it can act on and gives the thread back.

Stdlib-only by design: this sits underneath the writers and the
connectors and must not drag the package in behind it.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import threading
import time
import weakref
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0
"""How long :func:`vault_lock` waits before refusing.

Chosen below the ``/v1`` request timeout (30 s) so a queued caller is
answered with a *meaningful* "busy, retry" refusal rather than with the
generic timeout the middleware would otherwise produce.
"""

_UNSUPPORTED_FLOCK_ERRNOS: Final[frozenset[int]] = frozenset(
    {
        getattr(errno, name)
        for name in ("ENOTSUP", "EOPNOTSUPP", "ENOLCK", "EINVAL", "ENOSYS")
        if hasattr(errno, name)
    }
)
"""The errnos that mean *this filesystem cannot lock*, and only those.

Named explicitly because the guard used to be a bare ``except OSError``
whose comment claimed only "a mount that does not implement flock" while
the code also swallowed ``EPERM``/``EACCES`` (a permission
misconfiguration), ``EBADF`` (a bug in this module), and ``EINTR`` (a
retryable signal interruption) — each of them silently downgrading the
cross-process guarantee to nothing (#1603).

* ``ENOTSUP`` / ``EOPNOTSUPP`` — the classic "not implemented on this
  mount" answer. Distinct values on macOS, the same value on Linux; both
  are listed because the set is read, not compared to one platform.
* ``ENOLCK`` — no locks available, e.g. an NFS mount whose lock manager
  is not running.
* ``EINVAL`` — the descriptor does not support locking at all.
* ``ENOSYS`` — the syscall is absent.

Anything else propagates. The members are looked up defensively because
not every name is defined on every platform.
"""

_POLL_INTERVAL_SECONDS: Final[float] = 0.01
"""Gap between non-blocking ``flock`` attempts while waiting.

Short enough that an uncontended hand-off costs one poll, long enough
that a ten-second wait is a thousand syscalls rather than a spin.
"""


class VaultLockTimeoutError(TimeoutError):
    """Raised when :func:`vault_lock` could not take the lock in time.

    A :class:`TimeoutError` subclass so a caller that only cares that
    the work did not start can catch the stdlib type.
    """


class _LockHolder:
    """Weakref-able container for a per-path :class:`threading.Lock`.

    ``threading.Lock`` is a C-level object without a ``__weakref__``
    slot, so it cannot be the value of a
    :class:`weakref.WeakValueDictionary` directly. Wrapping it in a tiny
    Python object gives us the slot, and keeps the lock alive for as
    long as any caller holds the holder.
    """

    __slots__ = ("__weakref__", "lock")

    def __init__(self) -> None:
        """Initialise the holder with a fresh :class:`threading.Lock`."""
        self.lock = threading.Lock()


_THREAD_LOCK_HOLDERS: weakref.WeakValueDictionary[Path, _LockHolder] = (
    weakref.WeakValueDictionary()
)
"""Per-resolved-path lock holders.

Weak so a long-running server locking many vaults does not accumulate
dead entries. Correctness does not depend on the entry surviving: every
caller pins its holder with a strong local reference for the whole time
it holds the lock, so any *overlapping* caller — the only kind that
matters — is guaranteed to find the same live holder.
"""

_THREAD_LOCK_HOLDERS_GUARD = threading.Lock()
"""Guards the registry itself, so two threads cannot mint two holders."""


def _thread_lock_holder_for(path: Path) -> _LockHolder:
    """Return the per-path lock holder used to serialise threaded callers.

    The key is ``path.resolve()`` so two callers naming the same lock
    file differently (relative vs absolute, with or without a symlinked
    parent) share one holder. Without resolution the raw
    :class:`~pathlib.Path` objects compare unequal and would each get
    their own holder, silently breaking serialisation.

    Args:
        path: The lock file path, in whatever form the caller had it.

    Returns:
        The holder for that path, created on first use.
    """
    resolved = path.resolve(strict=False)
    with _THREAD_LOCK_HOLDERS_GUARD:
        holder = _THREAD_LOCK_HOLDERS.get(resolved)
        if holder is None:
            holder = _LockHolder()
            _THREAD_LOCK_HOLDERS[resolved] = holder
        return holder


def _timeout_error(lock_path: Path, timeout: float) -> VaultLockTimeoutError:
    """Build the refusal raised when the wait ran out.

    The message names the lock *file* and never the work behind it: the
    lock path is Creek's own, while a caller's payload is the vault
    owner's content.

    Args:
        lock_path: The lock file that stayed held.
        timeout: The deadline, in seconds, that expired.

    Returns:
        The error to raise.
    """
    return VaultLockTimeoutError(
        f"could not take the vault lock at {lock_path} within {timeout:g}s"
    )


def _acquire_flock(
    fileno: int,
    *,
    deadline: float,
    lock_path: Path,
    timeout: float,
) -> bool:
    """Take the exclusive advisory lock on *fileno*, polling to *deadline*.

    Args:
        fileno: An open descriptor on the lock file.
        deadline: A :func:`time.monotonic` instant to give up at.
        lock_path: The lock file, for the refusal message and the log.
        timeout: The caller's wait, restated in the refusal message.

    Returns:
        ``True`` when the OS lock is held and must be released, ``False``
        when this platform or filesystem could not provide one — in
        which case the caller is protected by the thread lock alone.

    Raises:
        VaultLockTimeoutError: If another holder kept the lock past
            *deadline*, or if a signal kept interrupting the call until
            the deadline passed.
        OSError: If ``flock`` failed for any reason other than the
            filesystem not implementing it — see
            :data:`_UNSUPPORTED_FLOCK_ERRNOS`.
    """
    if not _HAS_FCNTL:  # pragma: no cover - exercised only on Windows
        return False
    while True:
        try:
            fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise _timeout_error(lock_path, timeout) from None
            time.sleep(_POLL_INTERVAL_SECONDS)
        except InterruptedError:
            # EINTR: a signal arrived mid-syscall. Nothing about the
            # lock is known — retrying to the same deadline is the only
            # answer that neither abandons the OS lock nor waits past
            # what the caller asked for. PEP 475 already retries this
            # inside CPython, so this is belt-and-braces rather than a
            # path production reaches; it is here because the previous
            # bare ``except OSError`` silently *degraded* on it, turning
            # a retryable interruption into a permanent loss of the
            # cross-process guarantee (#1603).
            if time.monotonic() >= deadline:
                raise _timeout_error(lock_path, timeout) from None
        except OSError as error:
            if error.errno not in _UNSUPPORTED_FLOCK_ERRNOS:
                # EPERM, EACCES, EBADF and friends are not "this mount
                # has no flock" — they are a misconfiguration or a bug,
                # and answering them by quietly dropping the guarantee
                # is how a vault ends up unserialised without anyone
                # being told. They propagate; ``run_ingest`` already
                # reports an ``OSError`` per unit rather than crashing.
                raise
            # A mount that does not implement flock (some SMB/NFS
            # setups). Degrading to the thread lock is strictly better
            # than refusing every write on such a vault, and it is the
            # same degradation Windows already gets — but it is a real
            # loss of guarantee, so it is said out loud once per call.
            logger.warning(
                "advisory locking is unavailable for %s (errno %s); "
                "overlapping writers in other processes will NOT be serialised",
                lock_path,
                error.errno,
            )
            return False
        else:
            return True


@contextlib.contextmanager
def vault_lock(
    lock_path: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive advisory lock on *lock_path* for the block.

    Scope is the lock file, and nothing wider. Two callers naming
    different lock files never wait on each other, which is what keeps a
    connector-scoped lock from serialising unrelated vault work.

    The lock is released in a ``finally``, so an exception raised by the
    body does not strand it.

    Args:
        lock_path: The lock file. Its parent directory is created if it
            does not exist; the file itself is opened for append and
            never read or written.
        timeout: Seconds to wait for a contended lock before refusing.

    Yields:
        Nothing. The block runs with the lock held.

    Raises:
        VaultLockTimeoutError: If the lock was still held elsewhere when
            *timeout* expired.
    """
    holder = _thread_lock_holder_for(lock_path)
    deadline = time.monotonic() + timeout
    if not holder.lock.acquire(timeout=max(timeout, 0.0)):
        raise _timeout_error(lock_path, timeout)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            held = _acquire_flock(
                handle.fileno(),
                deadline=deadline,
                lock_path=lock_path,
                timeout=timeout,
            )
            try:
                yield
            finally:
                if held:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        holder.lock.release()
