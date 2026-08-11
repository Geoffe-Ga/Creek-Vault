"""Tests for :mod:`creek._fsio` — the short-write-safe I/O primitives (#987).

``os.write`` is allowed to write fewer bytes than it was handed. Every
raw ``os.write`` call site in the vault/save writers ignored the returned
count, so a short write silently truncated the file at its *final* path
(there is no tempfile + ``os.replace`` in those paths). These tests pin
the primitives that close that hole:

* :func:`creek._fsio.write_all` — loops until the whole buffer is drained,
  and raises rather than spinning when the descriptor stops making
  forward progress.
* :func:`creek._fsio.create_exclusive` — ``O_CREAT | O_EXCL`` create plus
  a fully-drained write, unlinking the partial file if anything goes
  wrong so a truncated body is never promoted to the real path.
* :func:`creek._fsio.atomic_write_text` — tempfile + ``os.replace``
  staging for whole-file *rewrites* (#1312). Unlike the two above it
  writes to a scratch path and promotes it, so a reader of the real path
  sees either the old content or the new one and never a half-written
  mixture. This is the write half of the consent log's durability
  contract: a torn rewrite is what manufactured the corrupt log that
  ``creek.consent`` now has to quarantine.

No ``InterruptedError`` test lives here on purpose: per PEP 475 a signal
that arrives after ``n > 0`` bytes is reported as a short *success*, so
``os.write`` cannot surface ``InterruptedError`` for a partial write.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

import pytest

from creek._fsio import atomic_write_text, create_exclusive, write_all

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ShortWriteController

# A distinctive tail makes a halved write unmistakable: a truncated file
# keeps the leading ``x`` run but loses ``TAIL``.
_PAYLOAD = b"x" * 400 + b"TAIL"


def _open_new(path: Path) -> int:
    """Create *path* exclusively and return the writable descriptor."""
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)


def test_write_all_drains_short_writes(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """Every byte lands on disk even when each ``os.write`` writes half."""
    short_write.halve()
    path = tmp_path / "halved.bin"

    fd = _open_new(path)
    try:
        write_all(fd, _PAYLOAD)
    finally:
        os.close(fd)

    assert path.read_bytes() == _PAYLOAD
    assert short_write.calls > 1


def test_write_all_drains_one_byte_at_a_time(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """A one-byte-per-call descriptor still yields the exact payload.

    Pins the offset arithmetic: exactly ``len(payload)`` calls are needed,
    so an off-by-one advance or a wrong loop condition changes the count.
    """
    short_write.one_byte()
    path = tmp_path / "dribble.bin"

    fd = _open_new(path)
    try:
        write_all(fd, _PAYLOAD)
    finally:
        os.close(fd)

    assert path.read_bytes() == _PAYLOAD
    assert short_write.calls == len(_PAYLOAD)


def test_write_all_raises_when_no_progress(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """A descriptor that writes nothing raises ``EIO`` instead of spinning."""
    short_write.stall()
    path = tmp_path / "stalled.bin"

    fd = _open_new(path)
    try:
        with pytest.raises(OSError) as excinfo:
            write_all(fd, _PAYLOAD)
    finally:
        os.close(fd)

    assert excinfo.value.errno == errno.EIO
    assert path.read_bytes() == b""


def test_write_all_accepts_empty_payload(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """An empty payload is a no-op: the loop is never entered.

    ``stall`` is installed so a spurious ``os.write(fd, b"")`` would be
    seen as zero forward progress and raise — the test would then fail
    loudly rather than quietly tolerate the wasted syscall.
    """
    short_write.stall()
    path = tmp_path / "empty.bin"

    fd = _open_new(path)
    try:
        write_all(fd, b"")
    finally:
        os.close(fd)

    assert short_write.calls == 0
    assert path.read_bytes() == b""


def test_create_exclusive_writes_all_bytes_under_short_writes(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """``create_exclusive`` drains the whole payload despite short writes."""
    short_write.halve()
    path = tmp_path / "created.md"

    create_exclusive(path, _PAYLOAD)

    assert path.read_bytes() == _PAYLOAD
    assert short_write.calls > 1


def test_create_exclusive_propagates_file_exists_when_path_taken(
    tmp_path: Path,
) -> None:
    """``FileExistsError`` propagates untouched and the old file survives.

    Both writers' collision-retry loops key on ``FileExistsError``, so
    swallowing or re-wrapping it would turn a counter-suffix retry into a
    hard failure. The existing bytes must also be left alone — the
    unlink-on-failure cleanup must not fire on a create that never
    happened.
    """
    path = tmp_path / "taken.md"
    path.write_bytes(b"original contents")

    with pytest.raises(FileExistsError):
        create_exclusive(path, _PAYLOAD)

    assert path.read_bytes() == b"original contents"


def test_create_exclusive_unlinks_partial_file_when_write_raises_midway(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """A mid-write ``ENOSPC`` leaves no half-written file at the real path."""
    short_write.fail_after_half(errno.ENOSPC)
    path = tmp_path / "nospace.md"

    with pytest.raises(OSError) as excinfo:
        create_exclusive(path, _PAYLOAD)

    assert excinfo.value.errno == errno.ENOSPC
    assert not path.exists()


def test_create_exclusive_unlinks_when_write_makes_no_progress(
    tmp_path: Path,
    short_write: ShortWriteController,
) -> None:
    """A stalled descriptor raises ``EIO`` and leaves no empty file behind."""
    short_write.stall()
    path = tmp_path / "stalled.md"

    with pytest.raises(OSError) as excinfo:
        create_exclusive(path, _PAYLOAD)

    assert excinfo.value.errno == errno.EIO
    assert not path.exists()


# ---------------------------------------------------------------------------
# atomic_write_text (#1312)
# ---------------------------------------------------------------------------


def _tmp_residue(directory: Path) -> list[str]:
    """Return the names of any leftover ``.tmp`` files in *directory*.

    Args:
        directory: Directory the atomic write targeted.

    Returns:
        Sorted names of every entry whose name ends in ``.tmp``.
    """
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_atomic_write_text_round_trips_content(tmp_path: Path) -> None:
    """The exact text handed in is the exact text read back."""
    target = tmp_path / "log.json"
    content = '{"records": []}\n'

    atomic_write_text(target, content)

    assert target.read_text(encoding="utf-8") == content


def test_atomic_write_text_replaces_existing_content(tmp_path: Path) -> None:
    """Writing over an existing file replaces it wholesale, with no remnant.

    The stale content is deliberately *longer* than the replacement: a
    naive in-place write would leave the tail of the old bytes behind,
    which is exactly the torn-file shape ``creek.consent`` quarantines.
    """
    target = tmp_path / "log.json"
    target.write_text("stale much longer previous content\n", encoding="utf-8")

    atomic_write_text(target, "fresh\n")

    assert target.read_text(encoding="utf-8") == "fresh\n"


def test_atomic_write_text_leaves_no_tmp_residue(tmp_path: Path) -> None:
    """A successful write leaves only the target file in the directory."""
    target = tmp_path / "log.json"

    atomic_write_text(target, "content\n")

    assert _tmp_residue(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir()] == ["log.json"]


def test_atomic_write_text_creates_an_owner_only_file(tmp_path: Path) -> None:
    """The staged file lands at 0o600, not this module's 0o644 default.

    ``mkstemp`` creates owner-only, and ``os.replace`` does not restore
    the target's previous mode. That divergence from
    ``_NEW_FILE_MODE`` is deliberate — the first consumer is the consent
    log, an operator-private audit record naming source paths and
    operator identity — so it is pinned rather than left incidental.
    """
    target = tmp_path / "log.json"

    atomic_write_text(target, "content\n")

    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_text_preserves_original_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rename leaves the previous content intact and no debris.

    This is the property the consent log depends on: an interrupted
    save must not be able to truncate the record of prior grants.
    """
    target = tmp_path / "log.json"
    original = "original content\n"
    target.write_text(original, encoding="utf-8")

    def _refuse(*_args: object, **_kwargs: object) -> None:
        """Stand in for ``os.replace`` on a filesystem that refuses it.

        Raises:
            OSError: Always, to simulate a refused rename.
        """
        raise OSError("rename refused")

    monkeypatch.setattr("creek._fsio.os.replace", _refuse)

    with pytest.raises(OSError, match="rename refused"):
        atomic_write_text(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == original
    assert _tmp_residue(tmp_path) == []
