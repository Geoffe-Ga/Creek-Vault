"""Torn-write-safe file primitives: short writes (#987), atomic replace (#1307).

``os.write`` is allowed to write *fewer* bytes than it was handed: the
POSIX ``write(2)`` contract only promises some forward progress, so a
signal, a nearly-full disk or a slow device can all cut a write short.
Code that discards the returned count silently truncates its file.

Both :mod:`creek.vault.writer` and :mod:`creek.save.writer` create notes
by writing straight to the *final* path (``O_CREAT | O_EXCL``, with no
tempfile + ``os.replace`` staging step), so a short write there does not
merely spoil a scratch file — it files a half-written note under the
real name, with no exception raised for anyone to notice.
:func:`write_all` and :func:`create_exclusive` live here instead of being
triplicated across those call sites, so the drain loop and the
partial-file cleanup have exactly one implementation to audit.

:func:`atomic_write_text` covers the *overwrite* case the two creation
helpers deliberately do not: ``Path.write_text`` truncates the target
before it writes, so an interrupted rewrite leaves a half-file under the
real name and the previous contents gone. Staging into a tempfile and
committing with ``os.replace`` means a reader sees either the whole old
file or the whole new one, never a splice of the two — the same
hardening merged for the vault index in #1307.

:func:`atomic_replace_path` is the same commit protocol for writers
that will not hand over their bytes: a third-party writer given a
*path* (``pyarrow.parquet.write_table``, say) opens and truncates the
target itself, so there is no string or descriptor for
:func:`atomic_write_text` to stage. This one yields a sibling scratch
path for such a writer to fill and commits it on a clean exit, which is
what keeps a killed ``creek link`` from destroying an
``embeddings.parquet`` that cost a corpus-wide embed to produce
(#1441).

Near-identical private implementations already exist at
``creek/vault/writer.py:476`` (``_atomic_write_text``) and
``creek/redact/cli_commands.py:597`` (``_atomic_write``). Converging
them onto this helper is deliberate follow-up work, not part of #1312:
each carries call-site-specific behaviour (a ``mkdir`` in the former, a
symlink-materialisation contract in the latter) that has to be unpicked
against its own tests.

Stdlib-only by design: this module sits underneath the writers and must
not drag any of the package in behind it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def atomic_write_text(path: Path, content: str) -> None:
    """Replace *path*'s contents with *content* in a single atomic step.

    *content* is staged in a uniquely named tempfile inside *path*'s own
    directory and committed with ``os.replace``, so a concurrent reader
    sees either the whole previous file or the whole new one and never a
    splice of the two. If the write or the rename fails, the original
    file is left byte-intact and the stage file is removed — unlike
    ``Path.write_text``, which truncates the target *before* it writes
    and so turns any interruption into permanent data loss.

    Two consequences of ``mkstemp`` + ``os.replace`` that callers must
    know about, both measured rather than assumed:

    - The resulting file mode is **0o600**, inherited from
      :func:`tempfile.mkstemp` and deliberately tighter than this
      module's ``_NEW_FILE_MODE`` (0o644). The first consumer is an
      operator-private audit record naming source paths and operator
      identity, so owner-only is the right default; ``write_text``
      would have produced 0o644.
    - ``os.replace`` does *not* preserve the target's existing mode, so
      a file that was 0o644 tightens to 0o600 on its next write through
      this helper. The change is a one-way narrowing — it can never
      widen a file that was already private.

    Deliberately does not ``mkdir``: as with :func:`create_exclusive`,
    the parent directory must already exist. The stage file has to live
    beside the target anyway (a cross-filesystem ``os.replace`` raises
    ``EXDEV``), and conjuring a directory tree here would silently
    absorb a caller's wrong path.

    Args:
        path: File to overwrite. Its parent directory must already
            exist and be writable, because the stage file is created
            there.
        content: The full text to write, encoded as UTF-8.

    Raises:
        OSError: Propagated untouched from the staging create, the
            write, or the rename — an unwritable directory
            (``EACCES``), a full disk (``ENOSPC``), and so on. Any
            stage file left behind is unlinked first, and a failure to
            unlink it is suppressed so the caller still sees the
            original error rather than the cleanup's.
    """
    # ``mkstemp`` runs outside the ``try`` on purpose: if it raises there
    # is no descriptor and no stage file to clean up, and ``tmp_name``
    # would be unbound in the ``finally``.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        # ``fdopen`` takes ownership of ``fd``, so the ``with`` closes it
        # exactly once on every path — including the write failing.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    finally:
        # After a successful ``os.replace`` the stage name is gone, so
        # the existence check is what makes this cleanup a no-op on the
        # happy path rather than a spurious ``FileNotFoundError``.
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _fsync_path(path: Path) -> None:
    """Flush *path*'s already-written bytes from the page cache to disk.

    Opened read-only on purpose: ``fsync`` needs a descriptor, not write
    access, and reopening for writing would risk truncating a file some
    other writer has just finished filling.

    Args:
        path: An existing file whose contents must be durable.

    Raises:
        OSError: From the open or the flush, propagated untouched.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_replace_path(path: Path) -> Iterator[Path]:
    """Yield a sibling scratch path, then commit it over *path*.

    For writers that take a *path* rather than bytes or a descriptor,
    which is why :func:`atomic_write_text` cannot serve them: a library
    such as ``pyarrow.parquet.write_table`` opens and truncates the
    destination itself, so pointing it at the real artifact destroys the
    previous one the instant the write begins — and a crash, a full disk
    or a ``SIGKILL`` part-way through then leaves nothing behind. Here
    the writer only ever touches the scratch file; the real path is
    swapped in with ``os.replace`` once the body returns cleanly, so a
    failure leaves the previous artifact byte-intact.

    The scratch file is a sibling of the target because ``os.replace``
    raises ``EXDEV`` across filesystems, and it is removed on both the
    success and the failure path so repeated failures cannot litter the
    directory. Its bytes are ``fsync``ed *before* the rename: committing
    a name to contents the kernel has not yet written back would trade a
    durable artifact for one a power loss can still empty.

    As with :func:`atomic_write_text`, the committed file inherits
    ``mkstemp``'s owner-only 0o600 rather than the writer's usual mode,
    and the parent directory must already exist.

    Args:
        path: Artifact to replace. Its parent directory must exist and be
            writable, because the scratch file is created there.

    Yields:
        The scratch path for the caller's writer to fill. It exists (and
        is empty) on entry, so a writer that refuses to create files
        still has a target.

    Raises:
        OSError: Propagated untouched from the staging create, the
            ``fsync`` or the rename. A failure to remove the scratch file
            is suppressed so the caller still sees the original error.
    """
    # ``mkstemp`` runs outside the ``try`` for the same reason as in
    # :func:`atomic_write_text`: a failure there leaves nothing to clean
    # up and ``tmp_name`` unbound.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        yield tmp_path
        _fsync_path(tmp_path)
        os.replace(tmp_name, path)
    finally:
        # A successful ``os.replace`` consumes the scratch name, and a
        # writer that cleans up after its own failure may already have
        # removed it, so the existence check is what keeps this a no-op
        # rather than a spurious ``FileNotFoundError``.
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
