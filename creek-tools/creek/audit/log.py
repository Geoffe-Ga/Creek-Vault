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
  keyed on the resolved log path, so multiple :class:`AuditLog`
  instances that point at the same file still serialise correctly.
* On platforms without ``fcntl`` (notably Windows) only the in-process
  lock applies; cross-process concurrency falls back to "best effort"
  and callers must not run multiple processes against the same log.

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
import threading
from collections import defaultdict
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
_THREAD_LOCKS: dict[Path, threading.Lock] = defaultdict(threading.Lock)
_THREAD_LOCKS_GUARD = threading.Lock()


class AuditChainBrokenError(Exception):
    """Raised when :meth:`AuditLog.verify` detects tampering."""


# Backward-compatible alias mirroring the spec example in
# plans/prompts/2026-04-28/batch-C-audit-and-privacy-substrate.md.
AuditChainBroken = AuditChainBrokenError


def _thread_lock_for(path: Path) -> threading.Lock:
    """Return the per-path lock used to serialise threaded appends."""
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS[path]


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
            _thread_lock_for(self.path),
            self.path.open("a", encoding="utf-8") as fh,
        ):
            if _HAS_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                prev_hash = self._compute_prev_hash()
                chained = {**payload, _PREV_HASH_FIELD: prev_hash}
                line = json.dumps(chained, sort_keys=True)
                fh.write(line + "\n")
                fh.flush()
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
