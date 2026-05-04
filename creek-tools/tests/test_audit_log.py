"""Tests for the tamper-evident JSONL audit log primitive.

The :class:`creek.audit.AuditLog` is the integrity backbone for every
compliance log in the project (purge, redaction, privacy overrides). The
tests in this module exercise three guarantees:

* Append is atomic across threads and processes (no losses, no
  half-written lines).
* The hash chain detects any post-hoc tampering with a clear exception.
* The schema is JSONL — the file remains byte-iterable even mid-run, so
  ``O_APPEND`` writes never trigger a read-modify-write cycle.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from creek.audit import AuditChainBroken, AuditLog

if TYPE_CHECKING:
    from pathlib import Path


def test_append_round_trips_payload(tmp_path: Path) -> None:
    """A single append is observable via read()."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"operation": "x", "i": 1})

    entries = list(log.read())
    assert len(entries) == 1
    assert entries[0]["operation"] == "x"
    assert entries[0]["i"] == 1


def test_append_writes_jsonl_one_entry_per_line(tmp_path: Path) -> None:
    """Each appended payload becomes one newline-terminated JSON object."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})
    log.append({"op": "second"})

    raw = log.path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "first"
    assert json.loads(lines[1])["op"] == "second"


def test_genesis_prev_hash_is_zero(tmp_path: Path) -> None:
    """The first entry has a genesis (all-zero) prev_hash."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})

    entry = next(iter(log.read()))
    assert entry["prev_hash"] == "0" * 64


def test_subsequent_prev_hash_matches_previous_line(tmp_path: Path) -> None:
    """The prev_hash of entry N equals sha256 of entry N-1's bytes."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})
    log.append({"op": "second"})

    raw = log.path.read_text(encoding="utf-8").splitlines()
    expected = hashlib.sha256(raw[0].encode("utf-8")).hexdigest()
    second_entry = json.loads(raw[1])
    assert second_entry["prev_hash"] == expected


def test_verify_passes_for_untampered_log(tmp_path: Path) -> None:
    """verify() returns silently when every prev_hash is intact."""
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append({"op": "x", "i": i})

    log.verify()


def test_verify_rejects_removed_first_entry(tmp_path: Path) -> None:
    """Removing the first line breaks the chain and verify() raises."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})
    log.append({"op": "second"})

    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[1] + "\n", encoding="utf-8")

    with pytest.raises(AuditChainBroken):
        log.verify()


def test_verify_rejects_modified_payload(tmp_path: Path) -> None:
    """Mutating a stored field breaks the chain at the next entry."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first", "value": 1})
    log.append({"op": "second"})

    lines = log.path.read_text(encoding="utf-8").splitlines()
    tampered_first = lines[0].replace('"value": 1', '"value": 999')
    log.path.write_text(tampered_first + "\n" + lines[1] + "\n", encoding="utf-8")

    with pytest.raises(AuditChainBroken):
        log.verify()


def test_verify_handles_empty_log(tmp_path: Path) -> None:
    """An empty log is trivially valid."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.verify()


def test_verify_handles_missing_log(tmp_path: Path) -> None:
    """A missing log is trivially valid (no entries to verify)."""
    log = AuditLog(tmp_path / "missing.jsonl")
    log.verify()


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    """read() on a missing log yields no entries."""
    log = AuditLog(tmp_path / "missing.jsonl")
    assert list(log.read()) == []


def test_concurrent_appends_lose_nothing(tmp_path: Path) -> None:
    """Eight threads each appending 100 entries lose no entries."""
    path = tmp_path / "audit.jsonl"

    def append_n(worker: int) -> None:
        log = AuditLog(path)
        for j in range(100):
            log.append({"op": "x", "worker": worker, "j": j})

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(append_n, range(8)))

    log = AuditLog(path)
    assert sum(1 for _ in log.read()) == 800
    log.verify()


def test_corrupt_line_raises_on_read(tmp_path: Path) -> None:
    """A malformed JSON line raises AuditChainBroken from verify()."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})
    log.path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(AuditChainBroken):
        log.verify()


def test_payload_with_prev_hash_is_rejected(tmp_path: Path) -> None:
    """Caller-supplied prev_hash is forbidden — the log owns the chain."""
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="prev_hash"):
        log.append({"op": "x", "prev_hash": "deadbeef"})


def test_creates_parent_directory(tmp_path: Path) -> None:
    """Append creates the parent directory tree if missing."""
    log = AuditLog(tmp_path / "deep" / "nested" / "audit.jsonl")
    log.append({"op": "first"})
    assert log.path.exists()


def test_repeated_appends_do_not_rescan_log_when_single_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long single-writer run reuses the cached prev_hash.

    Regression for the O(N²) ``_last_line`` rescan flagged on PR #193:
    the optimisation is to remember the last hash + post-write file
    size on the instance, and re-read the file only when a foreign
    process resizes it. We assert that after the first append the
    fallback reader is not invoked again.
    """
    from creek.audit import log as log_module

    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})

    rescans = {"count": 0}
    real_last_line = log_module._last_line

    def counting_last_line(path: object) -> object:
        rescans["count"] += 1
        return real_last_line(path)  # type: ignore[arg-type]

    monkeypatch.setattr(log_module, "_last_line", counting_last_line)

    for i in range(50):
        log.append({"op": "x", "i": i})

    assert rescans["count"] == 0
    log.verify()


def test_thread_lock_keyed_on_resolved_path(tmp_path: Path) -> None:
    """Two instances on equivalent paths share the same thread lock.

    Regression for PR #193 review: ``Path("audit.jsonl")`` and
    ``Path("./audit.jsonl")`` compare unequal but resolve to the same
    file. If the lock dictionary keyed on the raw ``Path`` they would
    each get their own ``Lock``, silently breaking cross-thread
    serialisation. We assert that the resolved-path keying makes the
    instances share a lock.
    """
    from creek.audit.log import _thread_lock_holder_for

    same_file = tmp_path / "audit.jsonl"
    relative_form = (tmp_path / "." / "audit.jsonl").resolve()
    holder_a = _thread_lock_holder_for(same_file)
    holder_b = _thread_lock_holder_for(relative_form)
    assert holder_a is holder_b
    assert holder_a.lock is holder_b.lock


def test_append_calls_fsync_for_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each append fsyncs the file descriptor before releasing the lock.

    Regression for PR #193 review: ``flush`` only pushes data into the
    OS page cache; a crash before kernel writeback can silently lose
    the last entry. We monkeypatch ``os.fsync`` and assert it is called
    exactly once per append, on a real fd.
    """
    from creek.audit import log as log_module

    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(log_module.os, "fsync", recording_fsync)

    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"op": "first"})
    log.append({"op": "second"})

    assert len(calls) == 2
    assert all(isinstance(fd, int) and fd >= 0 for fd in calls)


def test_cache_invalidated_when_foreign_writer_resizes_file(
    tmp_path: Path,
) -> None:
    """A foreign-process append (different instance) is detected on next write.

    Two ``AuditLog`` instances on the same path simulate two processes:
    instance A writes, instance B writes, then A writes again. A's cache
    is now stale (B grew the file in between); A must rescan to find
    the true last line, otherwise the chain breaks.
    """
    path = tmp_path / "audit.jsonl"
    log_a = AuditLog(path)
    log_b = AuditLog(path)

    log_a.append({"op": "a1"})
    log_b.append({"op": "b1"})
    log_a.append({"op": "a2"})

    AuditLog(path).verify()
    entries = [e["op"] for e in AuditLog(path).read()]
    assert entries == ["a1", "b1", "a2"]


def test_thread_lock_holder_collected_when_no_audit_log_references_it(
    tmp_path: Path,
) -> None:
    """The per-path lock holder is GC-eligible once no AuditLog pins it.

    Regression for PR #193 review (comment 4367360694 HIGH): the prior
    ``defaultdict[Path, Lock]`` accumulated one lock per distinct path
    forever, leaking memory in long-running daemons (and in test
    suites where every ``tmp_path`` is unique). Switching to a
    ``WeakValueDictionary`` keyed on a holder lets dead entries fall
    out as soon as the last :class:`AuditLog` for that path is
    collected.

    We assert the holder is *strongly* referenced while an AuditLog
    exists (so concurrent AuditLogs on the same path share the lock)
    and *weakly* referenced afterwards (so the entry is reclaimable).
    """
    import gc
    import weakref

    from creek.audit.log import _THREAD_LOCK_HOLDERS

    path = tmp_path / "gc-audit.jsonl"
    log = AuditLog(path)
    log.append({"op": "x"})

    resolved = path.resolve(strict=False)
    holder = _THREAD_LOCK_HOLDERS.get(resolved)
    assert holder is not None
    holder_ref = weakref.ref(holder)

    # While the AuditLog is alive the holder must stay reachable.
    del holder
    gc.collect()
    assert holder_ref() is not None
    assert _THREAD_LOCK_HOLDERS.get(resolved) is not None

    # Drop the AuditLog (the only strong reference) and the holder
    # becomes eligible for collection.
    del log
    gc.collect()
    assert holder_ref() is None
    assert _THREAD_LOCK_HOLDERS.get(resolved) is None
