"""Short-write-safe primitives for raw file-descriptor I/O (#987).

``os.write`` is allowed to write *fewer* bytes than it was handed: the
POSIX ``write(2)`` contract only promises some forward progress, so a
signal, a nearly-full disk or a slow device can all cut a write short.
Code that discards the returned count silently truncates its file.

Both :mod:`creek.vault.writer` and :mod:`creek.save.writer` create notes
by writing straight to the *final* path (``O_CREAT | O_EXCL``, with no
tempfile + ``os.replace`` staging step), so a short write there does not
merely spoil a scratch file — it files a half-written note under the
real name, with no exception raised for anyone to notice. These two
helpers live here instead of being triplicated across those call sites,
so the drain loop and the partial-file cleanup have exactly one
implementation to audit.

Stdlib-only by design: this module sits underneath the writers and must
not drag any of the package in behind it.
"""

from __future__ import annotations

import contextlib
import errno
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_EXCLUSIVE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
"""Create-or-fail open flags: never truncates or clobbers an existing file."""

_NEW_FILE_MODE = 0o644
"""Permission bits requested for files created by :func:`create_exclusive`."""


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, looping over short writes.

    Args:
        fd: An open, writable file descriptor.
        data: The payload to drain in full. An empty payload is a no-op
            and issues no syscall.

    Raises:
        OSError: With ``errno.EIO`` when a single ``os.write`` reports no
            forward progress, rather than spinning on a descriptor that
            has stopped accepting bytes; or with the underlying errno
            when the write itself fails. Either way the bytes already
            written stay on the descriptor — a caller that owns a
            freshly created file should discard it (see
            :func:`create_exclusive`).
    """
    # The memoryview keeps ``view[offset:]`` O(1). Slicing ``data``
    # directly would copy the unwritten remainder on every iteration,
    # which turns a dribbling descriptor quadratic on the 35k-fragment
    # write path.
    view = memoryview(data)
    total = len(view)
    offset = 0
    while offset < total:
        # No ``InterruptedError`` branch on purpose: per PEP 475
        # ``os.write`` retries the syscall itself on ``EINTR``, which the
        # kernel reports only when *zero* bytes were transferred. A
        # signal arriving after n > 0 bytes is reported instead as a
        # short *success* — precisely the case this loop drains — so
        # such a branch would be unreachable dead code. A genuine
        # ``OSError`` (``ENOSPC`` on a later pass, say) must propagate.
        written = os.write(fd, view[offset:])
        if written <= 0:
            msg = f"short write: {offset}/{total} bytes written"
            raise OSError(errno.EIO, msg)
        offset += written


def create_exclusive(path: Path, data: bytes) -> None:
    """Create *path* with ``O_CREAT|O_EXCL`` and write every byte of *data*.

    One caveat, on errno fidelity rather than data safety: the
    descriptor is closed in an inner ``finally``, so if :func:`write_all`
    raises (an ``ENOSPC``, say) and ``os.close`` then *also* raises, the
    close-time exception replaces the original and the caller sees the
    close errno instead of the true root cause. ``os.close`` deliberately
    does not retry on ``EINTR`` (PEP 475), so this is narrow but
    reachable. The unlink cleanup still fires in that case — the handler
    below catches any ``OSError`` subtype — so the guarantee that no
    half-written file survives at the real path is unaffected.

    A *successful* :func:`write_all` followed by a failing ``os.close``
    likewise unlinks the (complete) file and raises. That is deliberate
    fail-safe behaviour: a failing ``close`` can be the only signal of a
    delayed write-back failure, so suppressing it would trade an
    errno-fidelity gap for genuine silent data loss.

    Args:
        path: File to create; its parent directory must already exist.
        data: Full contents to write.

    Raises:
        FileExistsError: When *path* already exists. Propagated
            untouched: both writers' counter-suffix retry loops key on
            it, and the cleanup below deliberately does not fire for a
            file this call never created.
        OSError: When the payload cannot be drained. The partial file is
            unlinked before the error propagates, so a truncated body is
            never promoted to the real path.
    """
    fd = os.open(str(path), _EXCLUSIVE_CREATE_FLAGS, _NEW_FILE_MODE)
    try:
        # Closing in the inner ``finally`` releases the descriptor
        # before the cleanup unlink runs, however the drain ended.
        try:
            write_all(fd, data)
        finally:
            os.close(fd)
    except OSError:
        # Unlink failures are suppressed so the error already in flight
        # is the one the caller sees — the drain's ``ENOSPC``, say, or
        # the ``os.close`` failure that displaced it in the inner
        # ``finally`` (see the docstring caveat).
        with contextlib.suppress(OSError):
            path.unlink()
        raise
