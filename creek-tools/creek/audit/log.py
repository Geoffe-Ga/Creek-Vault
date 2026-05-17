"""Append-only JSONL audit log with a sha256 hash chain and locking.

Each appended entry is one JSON object on its own line; the object
carries a ``prev_hash`` field equal to ``sha256`` of the previous line's
bytes. The first line uses an all-zero genesis hash. Any post-hoc
tampering — line removal, payload mutation, reordering — invalidates
the chain and is surfaced by :meth:`AuditLog.verify` as an
:class:`AuditChainBroken` exception.

Concurrency:

* Cross-process safety on POSIX is provided by ``fcntl.flock`` on a
  freshly opened file descriptor for every append, so callers do not
  share state between processes.
* Cross-thread safety is provided by a module-level ``threading.Lock``
  keyed on the **resolved** log path, so ``Path("audit.jsonl")`` and
  ``Path("./audit.jsonl")`` correctly share a lock even though the raw
  ``Path`` objects compare unequal.
* On platforms without ``fcntl`` (notably Windows) only the in-process
  lock applies; cross-process concurrency falls back to "best effort"
  and callers must not run multiple processes against the same log.

Durability: every successful ``append`` calls ``os.fsync`` on the file
descriptor before releasing the flock, so a crash (power loss, OOM
kill) immediately after ``append`` returns does not silently lose the
last entry. For a tamper-evidence log a silent loss-without-detection
is the worst failure mode; ``fsync`` makes the durability boundary
explicit.

The threat model is "careless or hostile editor", not "well-funded
adversary"; the chain is integrity, not authentication. An attacker who
can rewrite the entire log can also recompute every hash. Real
authentication would need an off-host signature, which this module
intentionally does not provide.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import weakref
from typing import TYPE_CHECKING, Any

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

GENESIS_PREV_HASH = "0" * 64
"""Genesis hash for the first entry in a fresh chain."""

_PREV_HASH_FIELD = "prev_hash"


class _LockHolder:
    """Weakref-able container for a per-path :class:`threading.Lock`.

    ``threading.Lock`` is a C-level object without a ``__weakref__``
    slot, so it cannot be the value of a :class:`weakref.WeakValueDictionary`
    directly. Wrapping it in a tiny Python object gives us the slot and
    keeps the lock alive for as long as any :class:`AuditLog` instance
    references the holder.
    """

    __slots__ = ("__weakref__", "lock")

    def __init__(self) -> None:
        """Initialise the holder with a fresh :class:`threading.Lock`."""
        self.lock = threading.Lock()


_THREAD_LOCK_HOLDERS: weakref.WeakValueDictionary[Path, _LockHolder] = (
    weakref.WeakValueDictionary()
)
"""Per-resolved-path lock holders.

A :class:`weakref.WeakValueDictionary` so holders (and the locks they
own) can be reclaimed once no live :class:`AuditLog` references the
holder for a given path. Long-running daemons writing to many distinct
log paths therefore do not accumulate dead lock entries; the test
suite, where each ``tmp_path`` is unique, also stays bounded across
runs.

Each :class:`AuditLog` keeps a strong reference to its holder via
``AuditLog._lock_holder`` so the dict entry is pinned for the
instance's lifetime even when no append is currently in flight.
"""

_THREAD_LOCK_HOLDERS_GUARD = threading.Lock()


class AuditChainBrokenError(Exception):
    """Raised when :meth:`AuditLog.verify` detects tampering."""


AuditChainBroken = AuditChainBrokenError


def _thread_lock_holder_for(path: Path) -> _LockHolder:
    """Return the per-path lock holder used to serialise threaded appends.

    The key is ``path.resolve()`` so that two :class:`AuditLog`
    instances constructed with different representations of the same
    file (``"audit.jsonl"`` vs ``"./audit.jsonl"`` vs an absolute path)
    share the same holder. Without resolution the raw ``Path`` objects
    compare unequal and would each get their own holder, silently
    breaking cross-thread serialisation.

    Returning the holder (rather than the bare lock) lets the caller
    pin the entry in the :class:`weakref.WeakValueDictionary` for as
    long as it needs the lock — the dict's value would otherwise be
    eligible for GC the instant this function returns.
    """
    resolved = path.resolve(strict=False)
    with _THREAD_LOCK_HOLDERS_GUARD:
        holder = _THREAD_LOCK_HOLDERS.get(resolved)
        if holder is None:
            holder = _LockHolder()
            _THREAD_LOCK_HOLDERS[resolved] = holder
        return holder


def _hash_line(line: str) -> str:
    """Return the sha256 hex digest of *line* (no trailing newline)."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _last_line(path: Path) -> str | None:
    """Return the last non-empty line of *path*, or ``None`` when empty.

    The implementation reads the whole file because audit logs are
    append-only and the wall-clock cost is dominated by the lock and
    fsync rather than the I/O. A future optimisation could ``seek`` to
    the tail and walk backwards, but that adds branches without a
    measured win for the entry sizes this module sees.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw:
        return None
    lines = [line for line in raw.splitlines() if line]
    if not lines:
        return None
    return lines[-1]


class AuditLog:
    """Append-only JSONL log with a sha256 hash chain.

    The instance caches the most recently written line's hash and the
    file's post-write size so that repeated single-writer appends are
    O(1) rather than O(N) — a regression flagged on PR #193 for the
    vault writer's 10k-fragment ingest path. The cache is invalidated
    transparently when a different process or instance grows the file
    in between writes (size mismatch ⇒ rescan).

    Args:
        path: Filesystem path to the log file. Parent directories are
            created on first write.
    """

    def __init__(self, path: Path) -> None:
        """Store the resolved log path; defer all I/O until first use."""
        self.path = path
        self._cached_last_hash: str | None = None
        self._cached_size: int | None = None
        # Pin the per-path lock holder for the lifetime of this
        # AuditLog. Holding a strong reference keeps the
        # WeakValueDictionary entry alive so concurrent AuditLogs on
        # the same path share the same lock. When the last AuditLog
        # for a path is GC'd the holder (and lock) become eligible for
        # collection — long-running daemons writing to many distinct
        # paths therefore do not leak locks.
        self._lock_holder = _thread_lock_holder_for(path)

    def append(self, payload: dict[str, Any]) -> None:
        """Append *payload* as a JSON line, stamping in the chain hash.

        The caller MUST NOT supply a ``prev_hash`` field; the log owns
        the chain so callers cannot accidentally break it. The append
        is atomic per line on POSIX thanks to ``O_APPEND`` + flock.

        Args:
            payload: Caller-provided JSON-serialisable dict.

        Raises:
            ValueError: If *payload* contains a ``prev_hash`` key.
        """
        if _PREV_HASH_FIELD in payload:
            msg = "payload must not include the reserved 'prev_hash' field"
            raise ValueError(msg)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (
            self._lock_holder.lock,
            self.path.open("a", encoding="utf-8") as fh,
        ):
            if _HAS_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            # Invariant: the flock above is acquired iff control reaches
            # the ``try`` below. A failure of the ``flock`` call would
            # raise out of the ``with`` block before ``finally`` runs,
            # so the unlock there is the release for *this* lock — not
            # for whatever happens to be locked at the OS level.
            try:
                prev_hash = self._compute_prev_hash()
                chained = {**payload, _PREV_HASH_FIELD: prev_hash}
                line = json.dumps(chained, sort_keys=True)
                fh.write(line + "\n")
                fh.flush()
                # fsync inside the flock window: the lock guarantees no
                # other writer races us, and fsync guarantees the bytes
                # are durable before we release the lock or return to
                # the caller. Without fsync, a crash between flush and
                # the OS's lazy writeback would silently drop the entry
                # — the worst failure mode for a tamper-evidence log.
                os.fsync(fh.fileno())
                self._cached_last_hash = _hash_line(line)
                self._cached_size = self.path.stat().st_size
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _compute_prev_hash(self) -> str:
        """Return the chain hash for the next append.

        Uses the per-instance cache when the on-disk file size still
        matches the size we last wrote; otherwise falls back to a full
        ``_last_line`` rescan. The size check correctly invalidates the
        cache when a different process or another :class:`AuditLog`
        instance has appended in between.
        """
        on_disk_size = self.path.stat().st_size if self.path.exists() else 0
        if (
            self._cached_last_hash is not None
            and self._cached_size == on_disk_size
            and on_disk_size > 0
        ):
            return self._cached_last_hash
        last = _last_line(self.path)
        return GENESIS_PREV_HASH if last is None else _hash_line(last)

    def read(self) -> Iterator[dict[str, Any]]:
        """Yield every entry in the log as a dict, oldest first.

        Malformed lines are skipped (they would have been caught by
        :meth:`verify` already); pure read-side iteration must remain
        forgiving so consumers like dashboards can render whatever the
        chain still contains.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                stripped = raw_line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed audit line in %s",
                        self.path,
                    )

    def verify(self) -> None:
        """Walk the chain and raise :class:`AuditChainBroken` on tampering.

        A missing or empty log is considered trivially valid — there are
        no entries to disagree about. A line that fails to parse as
        JSON, a missing ``prev_hash`` field, or a mismatch against the
        recomputed sha256 of the prior line all raise.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            previous_line: str | None = None
            for index, raw_line in enumerate(fh):
                stripped = raw_line.rstrip("\n")
                if not stripped:
                    continue
                self._verify_line(stripped, previous_line, index)
                previous_line = stripped

    def _verify_line(
        self,
        line: str,
        previous_line: str | None,
        index: int,
    ) -> None:
        """Validate one chained line against its expected prev_hash.

        Args:
            line: The raw JSON line being validated.
            previous_line: The full bytes of the prior chain line, or
                ``None`` for the first entry.
            index: Zero-based index of *line* in the file, used for
                diagnostics in the raised exception.
        """
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"Audit line {index} is not valid JSON"
            raise AuditChainBrokenError(msg) from exc
        if _PREV_HASH_FIELD not in entry:
            msg = f"Audit line {index} is missing 'prev_hash'"
            raise AuditChainBrokenError(msg)
        expected = (
            GENESIS_PREV_HASH if previous_line is None else _hash_line(previous_line)
        )
        if entry[_PREV_HASH_FIELD] != expected:
            msg = (
                f"Audit chain broken at line {index}: "
                f"expected prev_hash {expected!r}, "
                f"found {entry[_PREV_HASH_FIELD]!r}"
            )
            raise AuditChainBrokenError(msg)
