"""Two overlapping ingests of one source unit write one file per id (#1603).

What actually races is **not** what the issue title says. Measured before
this test existed, journal-vs-drive is safe: the two routes load *different*
ledger files (:func:`~creek.ingest.pipeline.ledger_for_source` gives each
source type its own), so their ``source_key`` spaces are disjoint, and
``.id-index.jsonl`` is append-based rather than read-modify-write, so two
distinct ids appended concurrently both survive. Twenty-five overlapping
journal+drive runs into one shared fragment directory produced zero
anomalies.

What races is **two overlapping runs over the same source unit** — two
uploads of the same bytes, two journal saves of one ``external_id``, a
``/v1`` journal save against a concurrent ``creek ingest --type markdown``.
The cause is the check-then-act in
:meth:`~creek.vault.writer.VaultWriter._write_model`: it looks the id up in
the per-directory index and creates the file if it is absent. That section
is guarded by a **per-instance** :class:`threading.Lock`, and
:func:`~creek.ingest.pipeline.run_ingest` constructs a fresh
:class:`~creek.vault.writer.VaultWriter` on every call — so two overlapping
runs hold two different locks and the guard protects nothing between them,
not even inside one threadpool. The on-disk signature is #1590's:
``2026-08-22-journal-3.md`` *and* ``2026-08-22-journal-3-1.md``, two notes
carrying one fragment id.

Two halves are needed and both are here:

* a **threads** test, which is the reachable ``/v1`` topology — every write
  route runs its ingest through ``starlette.concurrency.run_in_threadpool``;
* a **two-process** test, which is the only one that can tell the shipped
  fix from one whose ``flock`` was deleted. ``creek ingest --type markdown``
  writes the same ledger and the same key space as the ``/v1`` journal
  route, in a different interpreter.

Every test here forces the overlap rather than hoping for it. A
:class:`threading.Barrier` (in-process) or a pair of marker files
(cross-process) rendezvouses the two writers *before* each
``_write_model`` call, so the two runs step through their units in
lockstep. It makes a slow runner **fail** rather than pass vacuously: a
rendezvous that times out is recorded and asserted against, never
skipped.

A rendezvous is not on its own enough to put two *processes* inside the
window at once — see :data:`_WIDEN_SECONDS`, which holds each child
between its id lookup and its file creation so both look the id up
before either has created it. With that in place the red is
deterministic: measured with the ``flock`` deleted, the cross-process
test failed 15 times in 15, having failed only 10 in 15 without it.

The journal-vs-drive negative control is green today and must stay green.
It pins that two *different* routes into one directory both complete —
two ledgers, two key spaces, every fragment written — which is the claim
the fix rests on: no lock was needed between them, so no lock between
them was added.

It is **not** a guard against the per-directory lock being widened later.
Journal and Drive markdown both route to ``01-Fragments/Notes``, so they
already share this lock, and a vault-wide one would leave every assertion
here passing. Nothing in the test suite measures throughput; the numbers
that back the scoping decision live in the
:data:`~creek.vault.writer.INDEX_LOCK_FILENAME` docstring and were taken
by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

import creek
from creek._fslock import vault_lock
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import run_ingest
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import INDEX_FILENAME, INDEX_LOCK_FILENAME, VaultWriter
from creek_mcp.tools.drive import _sync_lock_path

if TYPE_CHECKING:
    from collections.abc import Iterator

_UNITS: Final[int] = 6
"""Source units per overlapping run.

More than one, because the defect needs a *sequence* of writes to expose
its second half: the loser of the first race must go on to mint duplicates
for the units the winner wrote while the loser's cached index sat stale.
"""

_WIDEN_SECONDS: Final[float] = 0.05
"""How long each child pauses *inside* the check-then-act it is racing.

The rendezvous releases both children within a millisecond of each other,
but that alone does not put them inside the window at the same time: the
loser is often scheduled late enough that the winner's index entry is
already on disk, whereupon the incremental refresh rescues it and no
duplicate is minted. Measured with the ``flock`` deleted and only the
refresh left standing, the run came back green 3 times in 8 at six units
and 5 times in 15 at forty — the outcome is a property of the run, not of
the unit count, so more units buy nothing.

So the window is widened where a loaded box would widen it anyway:
between the id lookup and the file creation, by pausing inside
``_atomic_create``. Under the shipped fix the pause is taken with the
directory's index lock held, so the sibling simply waits and one file is
still written; with the lock removed both children look up an id neither
has created yet, every time. It is a pause, never a *skip*: nothing here
suppresses work, so a runner slow enough to make the pause irrelevant
still runs the same assertions.
"""

_RENDEZVOUS_TIMEOUT: Final[float] = 30.0
"""How long one writer waits at the barrier for its partner.

Generous, because exceeding it means the test could not arrange the
overlap it exists to measure — which is recorded and asserted on, not
silently tolerated.
"""

_CHILD_TIMEOUT: Final[float] = 120.0
"""How long the parent waits for a child interpreter to finish."""

_PACKAGE_ROOT: Final[Path] = Path(creek.__file__).resolve().parents[1]
"""The directory ``creek`` lives in, put on the child's ``PYTHONPATH``."""

_VAULT_DIRS: Final[tuple[str, ...]] = (
    "00-Creek-Meta/Processing-Log",
    "00-Creek-Meta/State/ingest",
    "01-Fragments/Notes",
    "10-Liminal/Orphaned",
)
"""The minimum scaffold :class:`~creek.vault.writer.VaultWriter` requires."""

_CHILD_SOURCE: Final[str] = '''\
"""Ingest one corpus into one vault, in lockstep with a sibling process."""

import json
import sys
import time
from pathlib import Path

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import run_ingest
from creek.vault.writer import VaultWriter

seat = int(sys.argv[1])
sync_dir = Path(sys.argv[2])
vault = Path(sys.argv[3])
source = Path(sys.argv[4])
report = Path(sys.argv[5])
timeout = float(sys.argv[6])
widen = float(sys.argv[7])

state = {"calls": 0, "degraded": False}
original = VaultWriter._write_model
create = VaultWriter._atomic_create


def _slow_create(target_dir, base_name, content):
    """Pause between the id lookup and the file creation, then create."""
    time.sleep(widen)
    return create(target_dir, base_name, content)


VaultWriter._atomic_create = staticmethod(_slow_create)


def _rendezvous(step):
    """Publish arrival at *step* and wait for the sibling to arrive too."""
    (sync_dir / f"{seat}-{step}").write_text("here", encoding="utf-8")
    sibling = sync_dir / f"{1 - seat}-{step}"
    deadline = time.monotonic() + timeout
    while not sibling.exists():
        if time.monotonic() >= deadline:
            state["degraded"] = True
            return
        time.sleep(0.001)


def _spy(self, model, target_dir, **kwargs):
    """Rendezvous before every write, then do the real one."""
    step = state["calls"]
    state["calls"] += 1
    _rendezvous(step)
    return original(self, model, target_dir, **kwargs)


VaultWriter._write_model = _spy

outcome = run_ingest(
    ingestor_cls=INGESTOR_REGISTRY["markdown"],
    source_type="markdown",
    input_path=source,
    vault_path=vault,
)
report.write_text(
    json.dumps(
        {
            "degraded": state["degraded"],
            "calls": state["calls"],
            "errors": [str(error) for error in outcome.errors],
        }
    ),
    encoding="utf-8",
)
'''
"""A child that ingests a corpus, stepping in time with its sibling."""


class _Lockstep:
    """A reusable two-party rendezvous that records its own failures.

    :class:`threading.Barrier` alone would be enough to *arrange* the
    overlap; it would not be enough to *prove* it happened. A barrier that
    times out breaks, every later wait raises immediately, and the run
    finishes looking exactly like a run that never contended. So each
    outcome is counted, and the test asserts on the counts.

    Attributes:
        rendezvous: How many times both parties met.
        degraded: ``True`` once any wait timed out or found the barrier
            already broken — the test must fail rather than report a
            green it did not earn.
    """

    def __init__(self, parties: int, timeout: float) -> None:
        """Arrange a rendezvous for *parties* writers.

        Args:
            parties: How many writers must meet at each step.
            timeout: Seconds one party waits for the others.
        """
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout
        self._lock = threading.Lock()
        self.rendezvous = 0
        self.degraded = False

    def wait(self) -> None:
        """Meet the other parties, or record that the meeting failed."""
        try:
            self._barrier.wait(timeout=self._timeout)
        except threading.BrokenBarrierError:
            with self._lock:
                self.degraded = True
        else:
            with self._lock:
                self.rendezvous += 1


def _make_vault(root: Path) -> Path:
    """Scaffold a minimal vault the writer can target.

    Args:
        root: Directory to create ``vault/`` inside.

    Returns:
        The vault root.
    """
    vault = root / "vault"
    for relpart in _VAULT_DIRS:
        (vault / relpart).mkdir(parents=True, exist_ok=True)
    return vault


def _make_corpus(directory: Path, prefix: str, units: int = _UNITS) -> Path:
    """Write *units* markdown notes into *directory*.

    Args:
        directory: Where the notes go. Created if absent.
        prefix: Filename and heading stem, which keeps two corpora distinct.
        units: How many notes to write.

    Returns:
        The directory, for use as an ``input_path``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(units):
        (directory / f"{prefix}-{index}.md").write_text(
            f"# {prefix} {index}\n\nRiver body text number {index} for {prefix}.\n",
            encoding="utf-8",
        )
    return directory


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment note under ``01-Fragments``, sorted.

    Args:
        vault: The vault root.

    Returns:
        The markdown files, in path order.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _fragment_ids(vault: Path) -> list[str]:
    """Return the ``id`` each fragment note declares, one per file.

    Read from the notes themselves rather than from ``.id-index.jsonl``:
    the index is the thing the defect corrupts, so trusting it here would
    make the assertion answer with the same stale claim that caused the
    bug. A file whose frontmatter declares no string id still contributes
    an entry, so a torn note cannot shrink the count into a false pass.

    Args:
        vault: The vault root.

    Returns:
        One id per fragment file, in path order.
    """
    ids: list[str] = []
    for path in _fragment_files(vault):
        post = frontmatter.load(str(path))
        declared = post.get("id")
        ids.append(declared if isinstance(declared, str) else f"<no-id:{path.name}>")
    return ids


def _lockstep_writes(
    monkeypatch: pytest.MonkeyPatch,
    parties: int,
) -> _Lockstep:
    """Make every :meth:`VaultWriter._write_model` call rendezvous first.

    The rendezvous sits *outside* the method, so whatever locking the
    method itself takes is still exercised — and cannot deadlock against
    the barrier, because no writer is holding a lock while it waits.

    Args:
        monkeypatch: pytest's patcher, which restores the method after.
        parties: How many concurrent writers must meet at each write.

    Returns:
        The rendezvous, for the test to assert overlap actually happened.
    """
    step = _Lockstep(parties, _RENDEZVOUS_TIMEOUT)
    original = VaultWriter._write_model

    def spy(
        writer: VaultWriter,
        model: Any,
        target_dir: Path,
        **kwargs: Any,
    ) -> Path:
        """Meet the other writers, then perform the real write."""
        step.wait()
        return original(writer, model, target_dir, **kwargs)

    monkeypatch.setattr(VaultWriter, "_write_model", spy)
    return step


def _ingest_concurrently(
    specs: list[tuple[Path, Path, str | None]],
) -> list[str]:
    """Run one ingest per spec, all at once, and collect what blew up.

    Args:
        specs: ``(source, vault, ledger_source)`` per concurrent run.

    Returns:
        Repr strings of anything raised out of a run. Empty is the
        expected outcome; the caller asserts on it so a crashed thread
        cannot masquerade as "no duplicates found".
    """
    failures: list[str] = []
    guard = threading.Lock()

    def worker(source: Path, vault: Path, ledger_source: str | None) -> None:
        """Run one ingest, recording any exception instead of losing it."""
        try:
            run_ingest(
                ingestor_cls=INGESTOR_REGISTRY["markdown"],
                source_type="markdown",
                input_path=source,
                vault_path=vault,
                ledger_source=ledger_source,
            )
        except Exception as error:
            with guard:
                failures.append(repr(error))

    threads = [threading.Thread(target=worker, args=spec) for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return failures


def _spawn_child(
    seat: int,
    *,
    sync_dir: Path,
    vault: Path,
    source: Path,
    report: Path,
) -> subprocess.Popen[bytes]:
    """Start one lockstepping ingest in its own interpreter.

    Args:
        seat: ``0`` or ``1`` — which side of the rendezvous this child is.
        sync_dir: Shared directory the two children publish arrivals in.
        vault: The vault both children write into.
        source: The corpus this child ingests.
        report: Where this child writes its JSON outcome.

    Returns:
        The running child.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_SOURCE,
            str(seat),
            str(sync_dir),
            str(vault),
            str(source),
            str(report),
            str(_RENDEZVOUS_TIMEOUT),
            str(_WIDEN_SECONDS),
        ],
        env={**os.environ, "PYTHONPATH": str(_PACKAGE_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _child_report(child: subprocess.Popen[bytes], report: Path) -> dict[str, Any]:
    """Reap *child* and return the outcome it recorded.

    A child that died inside the ingest would leave no report at all, and
    every assertion downstream would read the empty vault as "nothing
    duplicated" — the vacuous pass this helper exists to stop.

    Args:
        child: The process to wait for.
        report: The JSON file the child was told to write.

    Returns:
        The parsed report.
    """
    _, stderr = child.communicate(timeout=_CHILD_TIMEOUT)
    assert child.returncode == 0, stderr.decode("utf-8", "replace")
    detail = stderr.decode("utf-8", "replace")
    assert report.exists(), f"child wrote no report: {detail}"
    parsed: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
    return parsed


def test_two_overlapping_ingests_of_one_source_unit_write_one_file_per_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two threads ingesting one corpus must not mint two notes per id.

    This is the reachable ``/v1`` topology: every write route hands its
    ingest to ``run_in_threadpool``, so two overlapping saves are two
    threads of one process, each with its own
    :class:`~creek.vault.writer.VaultWriter` and therefore its own inert
    per-instance lock.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Installs the write-time rendezvous.
    """
    step = _lockstep_writes(monkeypatch, parties=2)
    vault = _make_vault(tmp_path)
    source = _make_corpus(tmp_path / "src", "journal")

    failures = _ingest_concurrently([(source, vault, None), (source, vault, None)])

    assert failures == []
    assert not step.degraded, "the two writers never met; overlap was not measured"
    assert step.rendezvous == _UNITS * 2

    ids = _fragment_ids(vault)
    assert len(ids) == len(set(ids)), f"two notes share one fragment id: {sorted(ids)}"
    assert len(ids) == _UNITS


def test_two_processes_ingesting_one_source_unit_write_one_file_per_id(
    tmp_path: Path,
) -> None:
    """The cross-process half, proved with two real interpreters.

    A ``/v1`` journal save and a concurrent ``creek ingest --type
    markdown`` write the same ledger and the same key space from two
    different processes. Only this test can distinguish the shipped fix
    from one that serialises threads alone: a :class:`threading.Lock` is
    invisible across a ``fork``/``exec``.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    source = _make_corpus(tmp_path / "src", "journal")
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    reports = [tmp_path / f"report-{seat}.json" for seat in range(2)]

    children = [
        _spawn_child(
            seat,
            sync_dir=sync_dir,
            vault=vault,
            source=source,
            report=reports[seat],
        )
        for seat in range(2)
    ]
    outcomes = [
        _child_report(child, report)
        for child, report in zip(children, reports, strict=True)
    ]

    for seat, outcome in enumerate(outcomes):
        assert outcome["errors"] == [], f"child {seat} reported ingest errors"
        assert not outcome["degraded"], f"child {seat} never met its sibling"
        assert outcome["calls"] == _UNITS

    ids = _fragment_ids(vault)
    assert len(ids) == len(set(ids)), f"two notes share one fragment id: {sorted(ids)}"
    assert len(ids) == _UNITS


def test_overlapping_journal_and_drive_ingests_do_not_interfere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: two *different* routes never needed a lock.

    Journal and Drive load different ledger files, so their ``source_key``
    spaces are disjoint and no unit is ever ingested twice. They do share
    one ``01-Fragments/Notes/.id-index.jsonl``, and that file is
    append-only, so both ids survive. Measured green before any fix
    existed and asserted green after: the premise the scoping decision
    rests on is that these two never needed serialising, and this is the
    assertion of it. It pins that both routes complete and both ledgers
    fill, not merely that nothing was corrupted.

    It does **not** detect the lock being widened later — both routes
    land in ``01-Fragments/Notes`` and so already share this key, and a
    vault-wide lock would leave every assertion below passing.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Installs the write-time rendezvous.
    """
    step = _lockstep_writes(monkeypatch, parties=2)
    vault = _make_vault(tmp_path)
    journal_src = _make_corpus(tmp_path / "journal-src", "journal")
    drive_src = _make_corpus(tmp_path / "drive-src", "drive")

    failures = _ingest_concurrently(
        [(journal_src, vault, None), (drive_src, vault, "gdrive")]
    )

    assert failures == []
    assert not step.degraded, "the two writers never met; overlap was not measured"
    assert step.rendezvous == _UNITS * 2

    ids = _fragment_ids(vault)
    assert len(ids) == len(set(ids)), f"two notes share one fragment id: {sorted(ids)}"
    assert len(ids) == _UNITS * 2

    for source_name in ("markdown", "gdrive"):
        ledger_path = SourceLedger.path_for(vault, source_name)
        assert ledger_path.exists(), f"{source_name} ledger was never written"
        raw = ledger_path.read_text(encoding="utf-8")
        rows = [line for line in raw.splitlines() if line.strip()]
        assert len(rows) >= _UNITS, f"{source_name} ledger recorded {len(rows)} rows"


def test_a_drive_sync_holding_its_connector_lock_can_still_write_fragments(
    tmp_path: Path,
) -> None:
    """The reentrancy audit: the two lock keys must never be the same one.

    :func:`~creek._fslock.vault_lock` fronts ``flock`` with a plain
    :class:`threading.Lock`, so it is **not** reentrant — taking it twice
    on one key in one thread self-deadlocks until the timeout expires.
    ``creek_mcp.tools.drive`` already holds ``gdrive.lock`` across its
    whole download-and-ingest window, so a fragment-write lock keyed on
    anything the connector also holds would burn the full wait and then
    refuse every sync.

    This drives a real ingest with that connector lock held, at a wait
    short enough that a nested acquisition of the *same* key could not
    hide inside it.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    source = _make_corpus(tmp_path / "src", "drive")
    # Taken from the connector's own helper, never re-derived here: a key
    # spelled out a second time in the test would keep agreeing with itself
    # after the connector moved its lock, which is precisely the collision
    # this test exists to notice.
    connector_lock = _sync_lock_path(vault)

    started = time.monotonic()
    with vault_lock(connector_lock, timeout=1.0):
        result = run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=source,
            vault_path=vault,
            ledger_source="gdrive",
        )
    elapsed = time.monotonic() - started

    assert result.errors == []
    assert len(_fragment_ids(vault)) == _UNITS
    assert elapsed < _RENDEZVOUS_TIMEOUT


_BLOCKED_PROBE_SECONDS: Final[float] = 0.5
"""How long a caller that must be blocked is watched before believing it.

Only ever used to confirm something did **not** finish. The lock is held
for the whole probe, so a correctly-blocked caller cannot finish inside
it no matter how slow the runner is; an unlocked one finishes in
single-digit milliseconds.
"""


def _journal_fragment(model_id: str) -> Fragment:
    """Build a native journal fragment with a fixed id.

    Args:
        model_id: The fragment id, which is also the index key under test.

    Returns:
        The model, routed to ``01-Fragments/Journal`` by its platform.
    """
    return Fragment(
        id=model_id,
        title="Day",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
    )


def test_an_in_place_update_waits_on_the_directory_index_lock(
    tmp_path: Path,
) -> None:
    """:meth:`VaultWriter.update_fragment` takes the same key a write takes.

    The in-place rewrite is the *other* half of idempotent ingest (#673):
    ``run_ingest`` calls it for every source unit whose content changed,
    and it performs its own check-then-act — locate the file mapped to
    the id, then rewrite it — through the same per-directory index. A
    lock on :meth:`~creek.vault.writer.VaultWriter._write_model` alone
    would leave that half racing a concurrent write of the same id.

    Proved by holding the directory's index lock and watching the update
    fail to proceed: with the acquisition removed from ``update_fragment``
    the rewrite lands while the lock is still held.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    fragment = _journal_fragment("frag-update0001")
    written = VaultWriter(vault_path=vault).write_fragment(fragment, body="original")
    lock_path = written.parent / INDEX_LOCK_FILENAME

    finished = threading.Event()
    rewritten: list[Path | None] = []

    def rewrite() -> None:
        """Rewrite the seeded fragment from a second writer."""
        rewritten.append(
            VaultWriter(vault_path=vault).update_fragment(fragment, "rewritten")
        )
        finished.set()

    worker = threading.Thread(target=rewrite)
    with vault_lock(lock_path):
        worker.start()
        blocked = not finished.wait(_BLOCKED_PROBE_SECONDS)
        held_body = written.read_text(encoding="utf-8")
    worker.join(timeout=_RENDEZVOUS_TIMEOUT)

    assert blocked, "update_fragment did not wait on the directory index lock"
    assert "original" in held_body, "the rewrite landed while the lock was held"
    assert not worker.is_alive()
    assert rewritten == [written]
    assert "rewritten" in written.read_text(encoding="utf-8")


def test_an_update_into_a_directory_that_never_existed_creates_nothing(
    tmp_path: Path,
) -> None:
    """A lookup that finds nothing must not leave a lock file behind.

    ``update_fragment`` answers ``None`` for an id no file maps to, and
    that answer is reached constantly — every *new* journal unit tries the
    update path first. Taking the lock before checking would mint an
    empty ``01-Fragments/Journal/`` holding one ``.id-index.lock`` in the
    operator's vault as a side effect of finding nothing, which is the
    same trap #1332 closed for the empty-index memo.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    fragment = _journal_fragment("frag-absent0001")
    writer = VaultWriter(vault_path=vault)
    target_dir = writer._fragment_target_dir(fragment)
    assert not target_dir.exists(), "the fixture vault already scaffolds the target"

    assert writer.update_fragment(fragment, "body") is None

    assert not target_dir.exists()


@pytest.fixture(autouse=True)
def _fail_on_zero_collection() -> Iterator[None]:
    """Guard against this module silently collecting nothing.

    Yields:
        Nothing; the fixture exists for its assertion at setup time.
    """
    assert _UNITS > 1, "the fixture corpus must hold more than one unit"
    yield


# ---- The refresh's fallbacks: when an incremental merge is not enough ----


def _index_file(vault: Path) -> Path:
    """Return the fragment index file the tests below manipulate.

    Args:
        vault: The vault root.

    Returns:
        The ``.id-index.jsonl`` path inside ``01-Fragments/Notes``.
    """
    return vault / "01-Fragments" / "Notes" / INDEX_FILENAME


def _index_record(model_id: str, filename: str) -> str:
    """Render one index record exactly as the writer appends it.

    Args:
        model_id: The fragment id the record maps.
        filename: The note it maps to.

    Returns:
        The framed JSON line, leading and trailing newline included.
    """
    return f"\n{json.dumps({'filename': filename, 'id': model_id}, sort_keys=True)}\n"


def _load_index(writer: VaultWriter, target_dir: Path) -> dict[str, str]:
    """Load *target_dir*'s index through the writer's own locked path.

    Args:
        writer: The writer whose cache is under test.
        target_dir: The fragment directory to load.

    Returns:
        The mapping the writer would answer lookups from.
    """
    with writer._lock:
        return dict(writer._load_index_locked(target_dir))


def test_an_index_rewritten_shorter_forces_a_full_reload(tmp_path: Path) -> None:
    """A file that shrank cannot be caught up by reading its tail.

    ``_persist_full_index`` replaces the file wholesale, and a cursor
    from before that rewrite points into content that no longer exists.
    Resuming there would splice unrelated bytes onto a stale mapping.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    target_dir = vault / "01-Fragments" / "Notes"
    index_path = _index_file(vault)
    index_path.write_text(
        _index_record("frag-aaa", "a.md") + _index_record("frag-bbb", "b.md"),
        encoding="utf-8",
    )
    writer = VaultWriter(vault_path=vault)
    assert _load_index(writer, target_dir) == {"frag-aaa": "a.md", "frag-bbb": "b.md"}

    index_path.write_text(_index_record("frag-ccc", "c.md"), encoding="utf-8")

    assert _load_index(writer, target_dir) == {"frag-ccc": "c.md"}


def test_an_index_that_appeared_since_the_scan_forces_a_full_reload(
    tmp_path: Path,
) -> None:
    """An empty directory that gained an index is re-read, not merged.

    The "no ids here" memo #1332 introduced must survive — a directory
    with no index file is not re-globbed on every lookup — but it must
    also *end* the moment another writer creates the file.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    target_dir = vault / "01-Fragments" / "Notes"
    writer = VaultWriter(vault_path=vault)
    assert _load_index(writer, target_dir) == {}

    _index_file(vault).write_text(_index_record("frag-ddd", "d.md"), encoding="utf-8")

    assert _load_index(writer, target_dir) == {"frag-ddd": "d.md"}


def test_a_damaged_index_is_never_topped_up_incrementally(tmp_path: Path) -> None:
    """A recovered mapping outranks the file, so its tail must not be replayed.

    Recovery re-derives the mapping by scanning the directory, and the
    scan deliberately overrides what the file claimed. Appending the
    file's later records onto that result would reinstate exactly the
    claims the scan disproved, so a damaged load is marked
    non-incremental and the next change costs a full reload.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    target_dir = vault / "01-Fragments" / "Notes"
    (target_dir / "real.md").write_text(
        "---\nid: frag-real\n---\n\nbody\n", encoding="utf-8"
    )
    index_path = _index_file(vault)
    index_path.write_text(
        "\n{ this is not json\n" + _index_record("frag-real", "wrong.md"),
        encoding="utf-8",
    )
    writer = VaultWriter(vault_path=vault)
    # The scan wins over the index's stale claim about ``frag-real``.
    assert _load_index(writer, target_dir) == {"frag-real": "real.md"}

    # The tail deliberately *re-asserts* the claim the scan disproved. An
    # append that only carried new ids would be merged to the same answer
    # either way, and the test would pass with the non-incremental guard
    # deleted — measured, so it is written this way on purpose.
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(_index_record("frag-real", "wrong.md"))
        handle.write(_index_record("frag-eee", "e.md"))

    reloaded = _load_index(writer, target_dir)
    assert reloaded["frag-eee"] == "e.md"
    assert reloaded["frag-real"] == "real.md"


def test_an_unparseable_tail_falls_back_to_the_full_recovery_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A torn append is repaired by the loader, not merged blindly.

    The tail is deliberately *partly* good: one intact record followed
    by a truncated one. A refresh that ignored the damage would fold the
    intact record in and leave the mapping looking healthy, so the
    evidence asserted here is the recovery itself — the directory scan
    and the warning that says it happened — and not merely the answer,
    which both paths would otherwise agree on.

    Args:
        tmp_path: pytest's per-test directory.
        caplog: Captured log records.
    """
    vault = _make_vault(tmp_path)
    target_dir = vault / "01-Fragments" / "Notes"
    for stem, model_id in (("kept", "frag-kept"), ("later", "frag-later")):
        (target_dir / f"{stem}.md").write_text(
            f"---\nid: {model_id}\n---\n\nbody\n", encoding="utf-8"
        )
    index_path = _index_file(vault)
    index_path.write_text(_index_record("frag-kept", "kept.md"), encoding="utf-8")
    writer = VaultWriter(vault_path=vault)
    assert _load_index(writer, target_dir) == {"frag-kept": "kept.md"}

    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(_index_record("frag-later", "WRONG.md"))
        handle.write('\n{"id": "frag-torn", "filen')

    with caplog.at_level("WARNING", logger="creek.vault.writer"):
        reloaded = _load_index(writer, target_dir)

    assert any("re-resolving by directory scan" in r.message for r in caplog.records)
    # The scan overrides the tail's stale claim; an incremental merge
    # would have adopted ``WRONG.md`` verbatim.
    assert reloaded == {"frag-kept": "kept.md", "frag-later": "later.md"}


def test_a_cached_index_with_no_cursor_is_rebuilt_rather_than_trusted(
    tmp_path: Path,
) -> None:
    """The cache is only ever trusted alongside a record of how far it read.

    The two are written together today, so this pins the invariant
    rather than a reachable path: a future edit that populates the
    mapping without the cursor must lose the cache, never silently keep
    serving a snapshot nobody can date.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _make_vault(tmp_path)
    target_dir = vault / "01-Fragments" / "Notes"
    _index_file(vault).write_text(_index_record("frag-fff", "f.md"), encoding="utf-8")
    writer = VaultWriter(vault_path=vault)
    writer._dir_indexes[target_dir] = {"frag-stale": "stale.md"}

    assert _load_index(writer, target_dir) == {"frag-fff": "f.md"}
