"""``creek._fslock`` — the vault's cross-process advisory lock (#1590).

Every claim this primitive makes is *measured* here rather than argued,
because the whole reason it exists is that the guarantee Creek already
had — a per-instance :class:`threading.Lock` — looked like serialisation
and was not one. In particular the cross-process promise is proved with
two **real subprocesses**: a threads-only test would pass identically
against a primitive that had no ``flock`` in it at all, which is exactly
the vacuous test this module was written to replace.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Final

import pytest

from creek import _fslock
from creek._fslock import VaultLockTimeoutError, vault_lock
from creek_mcp.httpapi.middleware.limits import DEFAULT_TIMEOUT_SECONDS

_CHILD_SOURCE: Final[str] = """\
import sys
import time
from pathlib import Path

from creek._fslock import vault_lock

lock_path, observed, ready = (Path(arg) for arg in sys.argv[1:4])
hold = float(sys.argv[4])

with vault_lock(lock_path, timeout=30.0):
    with observed.open("a", encoding="utf-8") as handle:
        handle.write("enter\\n")
    ready.write_text("held", encoding="utf-8")
    time.sleep(hold)
    with observed.open("a", encoding="utf-8") as handle:
        handle.write("exit\\n")
"""
"""A child that takes the lock, records its window, and holds it briefly."""

_PACKAGE_ROOT: Final[Path] = Path(_fslock.__file__).resolve().parents[1]
"""The directory ``creek`` lives in, put on the child's ``PYTHONPATH``."""

_CHILD_HOLD_SECONDS: Final[float] = 0.3
"""How long each child keeps the lock — long enough to overlap if it could."""

_CHILD_STARTUP_TIMEOUT: Final[float] = 30.0
"""How long a test waits for a child to report it holds the lock."""

_SHORT_TIMEOUT: Final[float] = 0.2
"""The deadline used when a test *wants* the acquisition to be refused."""

_THREADS: Final[int] = 8
"""How many threads pile onto one lock in the in-process contention test."""


def _spawn(
    lock_path: Path,
    observed: Path,
    ready: Path,
    *,
    hold: float = _CHILD_HOLD_SECONDS,
) -> subprocess.Popen[bytes]:
    """Start a child process that holds *lock_path* for *hold* seconds.

    Args:
        lock_path: The lock file the child contends for.
        observed: Append-only log the child writes ``enter``/``exit`` to.
        ready: Marker file the child creates once it holds the lock.
        hold: Seconds the child keeps the lock.

    Returns:
        The running child.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_SOURCE,
            str(lock_path),
            str(observed),
            str(ready),
            str(hold),
        ],
        env={**os.environ, "PYTHONPATH": str(_PACKAGE_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for(marker: Path) -> None:
    """Block until *marker* exists, or fail the test.

    Args:
        marker: The file the child creates once it holds the lock.
    """
    deadline = time.monotonic() + _CHILD_STARTUP_TIMEOUT
    while not marker.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"child never reported holding the lock: {marker}")
        time.sleep(0.01)


def _finish(child: subprocess.Popen[bytes]) -> None:
    """Wait for *child* and fail loudly if it did not exit cleanly.

    A child that died inside :func:`~creek._fslock.vault_lock` would
    leave an empty ``observed`` log, which every assertion below would
    read as "no overlap" — the vacuous pass this helper exists to stop.

    Args:
        child: The process to reap.
    """
    _, stderr = child.communicate(timeout=_CHILD_STARTUP_TIMEOUT)
    assert child.returncode == 0, stderr.decode("utf-8", "replace")


def _windows(observed: Path) -> list[str]:
    """Return the ``enter``/``exit`` events recorded in *observed*.

    Args:
        observed: The append-only log the children wrote.

    Returns:
        The events, in the order they were appended.
    """
    if not observed.exists():
        return []
    return [
        line.strip()
        for line in observed.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _refuse_flock(*_args: object) -> None:
    """Answer every ``flock`` the way an unsupporting mount does.

    Some SMB and NFS mounts raise :class:`OSError` rather than taking the
    lock. Two tests need that behaviour: one to pin the degradation
    itself, and one to take the OS lock out of the picture so the
    in-process half is what is under test.

    Raises:
        OSError: Always.
    """
    raise OSError(45, "Operation not supported")


class _Occupancy:
    """Records the greatest number of holders seen inside the lock at once.

    Attributes:
        peak: The largest concurrent occupancy observed.
        entries: How many times the guarded block was entered.
    """

    def __init__(self) -> None:
        """Start empty, with nothing having entered yet."""
        self._lock = threading.Lock()
        self._current = 0
        self.peak = 0
        self.entries = 0

    def enter(self) -> None:
        """Record one holder arriving."""
        with self._lock:
            self._current += 1
            self.entries += 1
            self.peak = max(self.peak, self._current)

    def leave(self) -> None:
        """Record one holder departing."""
        with self._lock:
            self._current -= 1


def test_two_processes_never_hold_the_vault_lock_at_once(tmp_path: Path) -> None:
    """The cross-process promise, proved with two real interpreters.

    This is the only test in the file that can distinguish the shipped
    primitive from one whose ``flock`` was deleted: a pure-Python thread
    lock is invisible across a ``fork``/``exec``, so a broken
    implementation interleaves the two children's windows here and
    nowhere else.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "meta" / "gdrive.lock"
    observed = tmp_path / "observed.log"
    children = [
        _spawn(lock_path, observed, tmp_path / f"ready-{index}") for index in range(2)
    ]
    for child in children:
        _finish(child)

    assert _windows(observed) == ["enter", "exit", "enter", "exit"]


def test_a_lock_held_by_another_process_is_refused_not_waited_on(
    tmp_path: Path,
) -> None:
    """A contended acquisition gives the thread back instead of pinning it.

    The deadline is what lets the request-timeout middleware above a
    ``/v1`` sync answer at all: a blocking ``LOCK_EX`` in a worker thread
    is not cancellable, so the caller would be told ``503`` while the
    thread stayed stuck on the lock.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "gdrive.lock"
    ready = tmp_path / "ready"
    child = _spawn(lock_path, tmp_path / "observed.log", ready, hold=2.0)
    _wait_for(ready)

    started = time.monotonic()
    with (
        pytest.raises(VaultLockTimeoutError, match=r"gdrive\.lock"),
        vault_lock(lock_path, timeout=_SHORT_TIMEOUT),
    ):  # pragma: no cover - the body is unreachable while the child holds it
        pytest.fail("the lock was taken while another process held it")
    waited = time.monotonic() - started

    # Refused near the deadline: it neither returned instantly (which
    # would mean it never really tried) nor waited out the child.
    assert _SHORT_TIMEOUT <= waited < 2.0
    _finish(child)


def test_threads_are_serialised_even_where_advisory_locking_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mount that refuses ``flock`` degrades loudly, and still serialises.

    Some SMB and NFS mounts answer ``flock`` with ``OSError``. Refusing
    every vault write on such a mount would be worse than the defect
    being fixed, so the lock falls back to the in-process half — but it
    says so, because the cross-process guarantee is genuinely gone.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: The active monkeypatch fixture.
        caplog: Captured log records.
    """

    monkeypatch.setattr(_fslock.fcntl, "flock", _refuse_flock)
    lock_path = tmp_path / "gdrive.lock"
    occupancy = _Occupancy()

    def _work() -> None:
        """Take the lock and record the occupancy while inside it."""
        with vault_lock(lock_path, timeout=_CHILD_STARTUP_TIMEOUT):
            occupancy.enter()
            time.sleep(0.005)
            occupancy.leave()

    threads = [threading.Thread(target=_work) for _ in range(_THREADS)]
    with caplog.at_level("WARNING", logger=_fslock.__name__):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert occupancy.entries == _THREADS
    assert occupancy.peak == 1
    assert any("advisory locking is unavailable" in r.message for r in caplog.records)


def test_threads_contending_for_one_lock_file_are_serialised(
    tmp_path: Path,
) -> None:
    """The in-process half, with the OS lock in place as it ships.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "gdrive.lock"
    occupancy = _Occupancy()

    def _work() -> None:
        """Take the lock and record the occupancy while inside it."""
        with vault_lock(lock_path, timeout=_CHILD_STARTUP_TIMEOUT):
            occupancy.enter()
            time.sleep(0.005)
            occupancy.leave()

    threads = [threading.Thread(target=_work) for _ in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert occupancy.entries == _THREADS
    assert occupancy.peak == 1


def test_two_different_lock_files_do_not_wait_on_each_other(
    tmp_path: Path,
) -> None:
    """Scope is the lock file, which is what keeps unrelated work parallel.

    A lock that serialised every vault operation would trade one
    correctness bug for a throughput one; this pins that two names
    genuinely overlap.

    Args:
        tmp_path: pytest's per-test directory.
    """
    both_inside = threading.Barrier(2, timeout=_CHILD_STARTUP_TIMEOUT)
    met: list[bool] = []

    def _work(name: str) -> None:
        """Hold one lock and wait there for the holder of the other.

        Args:
            name: The lock file's name.
        """
        with vault_lock(tmp_path / name, timeout=_CHILD_STARTUP_TIMEOUT):
            both_inside.wait()
            met.append(True)

    threads = [
        threading.Thread(target=_work, args=(name,))
        for name in ("gdrive.lock", "upload.lock")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert met == [True, True]


def test_the_same_lock_file_named_two_ways_is_one_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution, not spelling, decides which callers contend.

    Asserted with ``flock`` refused on purpose. Both spellings name one
    *inode*, so the OS lock catches them however the thread registry is
    keyed — which is why the first draft of this test stayed green with
    :meth:`~pathlib.Path.resolve` deleted from
    :func:`~creek._fslock._thread_lock_holder_for`, proving nothing about
    the half that carries the whole guarantee on Windows and on mounts
    that refuse ``flock``. Degrading first is what puts the registry
    under test.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setattr(_fslock.fcntl, "flock", _refuse_flock)
    direct = tmp_path / "gdrive.lock"
    indirect = tmp_path / "sub" / ".." / "gdrive.lock"
    (tmp_path / "sub").mkdir()
    occupancy = _Occupancy()
    release = threading.Event()

    def _hold() -> None:
        """Hold the lock under its plain name until released."""
        with vault_lock(direct, timeout=_CHILD_STARTUP_TIMEOUT):
            occupancy.enter()
            release.wait(timeout=_CHILD_STARTUP_TIMEOUT)
            occupancy.leave()

    holder = threading.Thread(target=_hold)
    holder.start()
    while occupancy.entries == 0:
        time.sleep(0.005)

    with (
        pytest.raises(VaultLockTimeoutError),
        vault_lock(indirect, timeout=_SHORT_TIMEOUT),
    ):  # pragma: no cover - the body is unreachable while the holder holds it
        pytest.fail("two spellings of one path did not contend")

    release.set()
    holder.join()
    assert occupancy.peak == 1


def test_the_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    """An exception inside the block must not strand either half of the lock.

    The thread half is asserted against a holder pinned *before* the
    raise. Without that strong reference the check is vacuous: the
    registry is a :class:`weakref.WeakValueDictionary`, so a stranded
    holder is simply collected once the failed call's frame dies and the
    next caller mints a fresh, unlocked one — a stranded lock and a
    released one then look identical from outside. Pinning it makes the
    ``finally`` that releases it the thing under test.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "gdrive.lock"
    sentinel = RuntimeError("ingest blew up")
    pinned = _fslock._thread_lock_holder_for(lock_path)

    with (
        pytest.raises(RuntimeError) as raised,
        vault_lock(lock_path, timeout=_SHORT_TIMEOUT),
    ):
        raise sentinel
    assert raised.value is sentinel
    assert not pinned.lock.locked(), "the thread half stayed held after the raise"

    # Immediately re-acquirable, in this thread and from another process.
    with vault_lock(lock_path, timeout=_SHORT_TIMEOUT):
        assert pinned.lock.locked(), "the re-acquisition took a different lock"
    observed = tmp_path / "observed.log"
    _finish(_spawn(lock_path, observed, tmp_path / "ready", hold=0.0))
    assert _windows(observed) == ["enter", "exit"]


def test_the_lock_files_parent_directory_is_created(tmp_path: Path) -> None:
    """The meta directory need not exist before the first sync locks it.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "00-Creek-Meta" / "State" / "ingest" / "gdrive.lock"
    assert not lock_path.parent.exists()

    with vault_lock(lock_path, timeout=_SHORT_TIMEOUT):
        assert lock_path.exists()


def test_the_default_wait_is_the_one_the_docstring_claims(tmp_path: Path) -> None:
    """The bare ``vault_lock(path)`` call, which no caller in-tree makes yet.

    Every other test here — and the only production caller — passes an
    explicit ``timeout``, so the signature default was reachable by
    nobody and asserted by nothing while its docstring stated a specific
    relationship to the ``/v1`` request budget. That relationship is
    pinned here rather than left as prose: a default at or above the
    middleware's own deadline would answer a queued sync with the
    generic timeout instead of the retryable refusal this whole path
    exists to produce.

    Args:
        tmp_path: pytest's per-test directory.
    """
    lock_path = tmp_path / "gdrive.lock"
    pinned = _fslock._thread_lock_holder_for(lock_path)

    with vault_lock(lock_path):
        assert pinned.lock.locked()
    assert not pinned.lock.locked()

    bound = inspect.signature(vault_lock).parameters["timeout"].default
    assert bound == _fslock.DEFAULT_LOCK_TIMEOUT_SECONDS
    assert 0 < _fslock.DEFAULT_LOCK_TIMEOUT_SECONDS < DEFAULT_TIMEOUT_SECONDS
