"""The containment walk must not report a subtree it could not list (#1498).

``creek._containment.find_escaping_symlink`` walks with
``os.walk(root, followlinks=False)`` and **no** ``onerror`` handler. ``os.walk``
swallows a failed ``scandir`` silently, so a directory the process cannot list
is indistinguishable from a directory that is empty. The function then returns
``None`` and its docstring's promise — "``None`` when every symlink under
*root* resolves inside it" — is a guarantee the code cannot honour.

Measured at HEAD, before a line of this file was written::

    src/locked/link.md -> outside/secret.md
    os.chmod(src/locked, 0o000)

    find_escaping_symlink(src)      -> None
    assert_source_contained(src)    -> returns normally (ADMITTED)
    sorted(src.rglob("*"))          -> [src/locked]        # link never seen
    os.walk(src, onerror=errs.append)
                                    -> PermissionError(errno=13, .../src/locked)

The last line is the whole issue: the information exists and is being
discarded. The module's own policy at ``creek/_containment.py:31`` is
"Unprovable containment IS an escape", and ``_resolved_target`` already
refuses an ``EACCES`` hit encountered *during resolution*. The identical
physical condition hit *during the walk* silently admits. That internal
inconsistency, not a policy disagreement, is what this file pins.

**The rulings these tests encode are deliberately NOT uniform**, and each is
pinned by a named assertion so that a later "make it consistent" sweep has to
argue with a test rather than quietly flip one of them:

* :func:`creek._containment.assert_source_contained` — the ingest write-path
  gate all eleven ingestors inherit — **reports** (WARNING) and continues. No
  leak is possible there: a subtree the gate cannot list is one the ingestor
  cannot list either. Refusing would turn one chmod-000 ``.Trashes`` into a
  permanent outage of the unattended ``creek sync`` loop, which is the outage
  #1444 measured and ``creek/cli.py:483-487`` states the policy against.
* ``creek redact --apply`` / ``--review`` — the redaction **write** path —
  **refuses** (exit 1). It is reached only from the interactive CLI, it
  destroys bytes irreversibly, and it has no report channel: printing
  "Applied redactions to N file(s)" over a region it could not examine is a
  false assurance from a safety tool.
* ``creek redact --scan`` — the redaction **read** path, which
  ``Pipeline._run_redaction`` also reaches on the ``creek process`` /
  ``creek sync`` path — **reports** (WARNING) and exits 0, matching the
  skip-and-count policy #1360 set for that surface.

The unlistable-subtree arm is the only thing that changes. An actual escaping
link still raises :class:`~creek._containment.EscapingSymlinkError` from the
ingest gate and still exits 1 from the redact write path; those contracts are
held by ``tests/test_ingest_symlink_containment.py`` and the SEC-003 block of
``tests/test_cli_redact.py``, neither of which this work modifies.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

runner = CliRunner()


_OUT_OF_TREE_SENTINEL = "CANARY-UNLISTABLE-1498-4d2b"
"""String that exists ONLY in the file parked outside the walked root.

Its *absence* from every log record is the #1087 no-oracle invariant carried
onto the new code path: a refusal — or a warning — must never name the place
the link pointed to.
"""

_IN_ROOT_MARKER = "CONTROL-IN-ROOT-1498-91ac"
"""String carried by the innocent, genuinely readable in-root file.

Every fixture here plants one. A tree whose only content sits behind the
locked directory would satisfy "the sentinel never appeared" perfectly while
proving nothing, which is the vacuity trap this marker exists to spring.
"""


# ---------------------------------------------------------------------------
# HAZARD 1: the fixture, written and proven BEFORE anything uses it
#
# Making a subtree unlistable means chmod 000 on a directory. A fixture that
# restores permissions only on the success path leaves a chmod-000 directory
# behind on every failed run — which breaks pytest's own tmp_path cleanup,
# every later test that walks that tree, and `git clean`. Teardown is
# therefore UNCONDITIONAL (a `finally` around the `yield`), and the proof that
# it survives a mid-test failure is a test in its own right, below.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_unlistable() -> Iterator[Callable[[Path], Path]]:
    """Yield a helper that chmods a directory to 000 and ALWAYS restores it.

    Four properties carry the safety argument:

    * ``try`` wraps the ``yield``, so ``finally`` runs on pass, on failure, on
      collection error inside the test body, and on ``KeyboardInterrupt``.
    * restoration is ``reversed``, so a locked directory nested inside another
      locked directory unwinds outside-in and the inner ``chmod`` is reachable.
    * ``suppress(OSError)`` stops a directory the test itself removed from
      masking the real assertion failure with a teardown error.
    * two ``pytest.skip`` guards stop the whole battery going *vacuously*
      green. Under ``root`` the mode bits are ignored, and some CI mounts
      ignore them too; either way the directory stays listable, every "the
      walk could not see it" assertion becomes trivially satisfiable, and a
      skip is the honest answer. The guards are checked by re-reading
      ``os.access`` rather than by trusting the ``chmod``.

    Yields:
        A callable taking the directory to lock and returning it, so a test
        can write ``locked = make_unlistable(src / "locked")``.
    """
    locked: list[Path] = []

    def _lock(path: Path) -> Path:
        """Make *path* unlistable, or skip when the platform will not allow it."""
        if os.geteuid() == 0:
            pytest.skip("running as root: mode bits do not make a directory unlistable")
        os.chmod(path, 0o000)
        if os.access(path, os.R_OK):
            os.chmod(path, 0o700)
            pytest.skip("this filesystem does not enforce directory mode bits")
        locked.append(path)
        return path

    try:
        yield _lock
    finally:
        for path in reversed(locked):
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700)


_TEARDOWN_PROOF_MODULE = '''
"""Generated throwaway module: locks a directory, then fails on purpose."""

from __future__ import annotations

import os
from pathlib import Path

from tests.test_containment_unreadable_subtree import make_unlistable

__all__ = ["make_unlistable"]


def test_locks_then_fails(make_unlistable) -> None:
    """Lock the handed-in directory and then fail, exercising teardown."""
    target = Path(os.environ["CREEK_1498_LOCK_DIR"])
    make_unlistable(target)
    assert not os.access(target, os.R_OK), "the fixture did not lock the directory"
    raise AssertionError("deliberate failure inside the locked-directory test")
'''
"""Source of the child test module used to prove the teardown is unconditional.

Run in a SUBPROCESS rather than through pytest's ``pytester`` fixture:
``pytester`` requires ``pytest_plugins = ["pytester"]`` in ``tests/conftest.py``,
which another lane holds exclusively this wave.

Left in the default (unit) lane rather than marked ``integration``: it costs
0.4s, and a marker would deselect the one test that proves the fixture cannot
poison the worktree from the lane every other test in this file runs in.
"""


def test_the_unlistable_fixture_restores_permissions_when_the_test_fails(
    tmp_path: Path,
) -> None:
    """The teardown must survive a mid-test failure, not just a passing run.

    A fixture that restores permissions on the happy path is worse than no
    fixture at all: the one run that leaves a chmod-000 directory behind is by
    definition a run that already failed, and the operator then debugs a
    poisoned worktree instead of the original failure.

    Watching this file's own tests pass proves nothing about that arm, so the
    proof is a child pytest process whose test locks a directory and then
    raises. The parent asserts both halves: the child really failed (so the
    teardown ran on the failure path, not the success path), and the directory
    is listable again *from the parent*, after the child exited.

    ``-o addopts=`` clears the project's ``--cov-fail-under=90``: without it
    the child's non-zero exit would be explained by coverage rather than by
    the deliberate ``AssertionError``, and the first assertion would be
    satisfied for the wrong reason.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "inside.txt").write_text(_IN_ROOT_MARKER, encoding="utf-8")
    child = tmp_path / "test_generated_teardown_proof.py"
    child.write_text(textwrap.dedent(_TEARDOWN_PROOF_MODULE), encoding="utf-8")

    env = dict(os.environ)
    env["CREEK_1498_LOCK_DIR"] = str(victim)
    # Fixed argv, no shell, this interpreter: the child is a file this test
    # just wrote, not anything an input could steer.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(child),
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if "skipped" in completed.stdout and completed.returncode == 0:
        pytest.skip("the child run skipped: this platform does not enforce mode bits")

    assert completed.returncode != 0, (
        "the child pytest run passed, so the deliberate failure never "
        "happened and this test proves nothing about the failure path.\n\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "deliberate failure" in completed.stdout, (
        "the child failed for some reason other than the planted assertion, "
        "so the fixture may never have locked anything.\n\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert os.access(victim, os.R_OK | os.X_OK), (
        "a FAILED test left a chmod-000 directory on disk. Every later test "
        "that walks this tree, pytest's own tmp_path cleanup, and `git clean` "
        "are all broken by it, and the operator debugs the poisoned worktree "
        f"instead of the original failure.\n\n{completed.stdout}"
    )
    assert sorted(p.name for p in victim.iterdir()) == ["inside.txt"], (
        "the directory reports as accessible but cannot be listed, so the "
        "restore was partial."
    )


# ---------------------------------------------------------------------------
# The tree every test below plants
# ---------------------------------------------------------------------------


def _plant_locked_escape(base: Path) -> tuple[Path, Path]:
    """Build ``src`` with a readable control file and a locked escaping subtree.

    The locked directory holds a symlink out of the tree. That link is the
    thing the walk is supposed to be able to promise about, and — until the
    directory is locked — genuinely does find; the companion assertion in each
    test that unlocks and re-walks is what proves the fixture is not vacuous.

    Args:
        base: Per-test scratch directory.

    Returns:
        ``(src, locked)`` — the source root and the directory to lock.
    """
    outside = base / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(
        f"# Secret\n\n{_OUT_OF_TREE_SENTINEL}\n",
        encoding="utf-8",
    )
    src = base / "src"
    locked = src / "locked"
    locked.mkdir(parents=True)
    (src / "plain.md").write_text(
        f"# Plain\n\n{_IN_ROOT_MARKER}\n",
        encoding="utf-8",
    )
    (locked / "link.md").symlink_to(outside / "secret.md")
    return src, locked


# ---------------------------------------------------------------------------
# 1. The walk must report what it could not see
# ---------------------------------------------------------------------------


def test_inspect_tree_reports_a_subtree_it_could_not_list(
    tmp_path: Path,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED. The walk owes its callers the fact that it was refused.

    ``find_escaping_symlink`` collapses "no escaping link exists" and "an
    entire subtree could not be read" into the same ``None``. The two callers
    weigh those facts differently — the ingest gate reports, the redact write
    path refuses — so one boolean cannot serve both, and a walk that discards
    the ``scandir`` failure leaves neither caller able to choose.

    Asserted on the reported directory, not merely on truthiness: a report
    that says "something, somewhere" is not actionable, and the operator needs
    the path they can run ``chmod`` on.

    Args:
        tmp_path: Pytest-provided temporary directory.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    from creek._containment import inspect_tree

    src, locked = _plant_locked_escape(tmp_path)
    escaping_link = locked / "link.md"

    before = inspect_tree(src)
    assert before.escaping == escaping_link, (
        "the walk does not find the escaping link even while the directory "
        "is readable, so locking it below could not change any answer and "
        f"this test would be vacuous.\n\n{before}"
    )
    assert before.unlistable == (), (
        f"a readable tree was reported as unlistable.\n\n{before}"
    )

    make_unlistable(locked)
    report = inspect_tree(src)

    assert report.unlistable == (locked,), (
        "the walk reported nothing about a directory whose scandir was "
        "refused. os.walk swallows the error when no onerror handler is "
        "passed, so an unlistable subtree is indistinguishable from an empty "
        f"one and the containment answer is a guess.\n\n{report}"
    )
    assert report.escaping is None, (
        "the walk claims to have found the escaping link inside a directory "
        "it could not list, which is impossible; the fixture is not doing "
        f"what this test believes.\n\n{report}"
    )


# ---------------------------------------------------------------------------
# 2. The ingest gate REPORTS. It must not refuse, and must not stay silent.
# ---------------------------------------------------------------------------


def test_assert_source_contained_warns_but_admits_an_unlistable_subtree(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED on the warning; the admission is the deliberate half.

    Two assertions that only mean anything together.

    **It must not raise.** No leak is possible on this arm: a subtree the gate
    cannot list is one the ingestor cannot list either — ``rglob`` returned
    only the locked directory itself, and ``CodeIngestor``'s ``iterdir()``
    raises ``EACCES`` straight into ``_discover_safe``. The fail-open costs a
    false *guarantee*, not a false *admission*. Refusing would instead turn one
    chmod-000 ``.Trashes`` or root-owned ``.git/objects`` into a permanent,
    silent failure of the unattended ``creek sync`` loop — the outage #1444
    measured, and the policy ``creek/cli.py:483-487`` states in the codebase's
    own words.

    **It must not stay silent.** A safety gate that cannot prove its answer and
    says nothing is the #1087 hazard restated. The durable channel already
    exists on this path — ``IngestResult.discovery_complete`` disarms the tomb
    sweep at ``creek/ingest/pipeline.py:1330`` — and this WARNING is what tells
    the operator which directory to fix.

    The escaping-link arm is untouched by any of this and still raises; that
    contract lives in ``tests/test_ingest_symlink_containment.py``.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log-capture fixture.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    from creek._containment import EscapingSymlinkError, assert_source_contained

    src, locked = _plant_locked_escape(tmp_path)
    make_unlistable(locked)

    with caplog.at_level(logging.WARNING, logger="creek._containment"):
        try:
            assert_source_contained(src)
        except EscapingSymlinkError as exc:  # pragma: no cover - failure path
            pytest.fail(
                "the ingest gate refused a source tree merely because one "
                "subtree could not be listed. Nothing out-of-tree can be read "
                "through a directory the walk cannot open, and refusing here "
                "makes one unreadable directory a permanent outage of the "
                f"unattended `creek sync` loop.\n\n{exc}"
            )

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(locked) in message for message in warnings), (
        "the gate admitted a tree it could not fully walk and said nothing. "
        "Its docstring promises `None` means every symlink beneath the root "
        "resolves inside it; with an unlistable subtree that promise is false "
        "and the operator has no way to learn it. Name the directory so it "
        f"can be fixed.\n\nwarnings={warnings}"
    )


def test_the_unlistable_warning_never_names_the_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED. The new code path inherits #1087's no-oracle invariant.

    Every existing containment message names the link *as walked* and never
    where it resolves to, because disclosing the target is the exfiltration
    oracle #1087 closes. A new report added on the same module must not be the
    place that invariant is quietly dropped — and an unlistable-subtree
    warning is exactly where it would be, since the tempting message is "could
    not check the link to <target>".

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log-capture fixture.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    from creek._containment import assert_source_contained

    src, locked = _plant_locked_escape(tmp_path)
    outside = tmp_path / "outside"
    make_unlistable(locked)

    with caplog.at_level(logging.DEBUG, logger="creek._containment"):
        assert_source_contained(src)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged.strip(), (
        "nothing at all was logged, so the two assertions below are "
        "satisfied by silence rather than by a well-formed report."
    )
    assert _OUT_OF_TREE_SENTINEL not in logged, (
        f"the log quoted content from outside the tree.\n\n{logged}"
    )
    assert "secret.md" not in logged, (
        "the log named the file the link resolves to. That is the oracle "
        f"#1087 closed: the refusal must not leak what it refused.\n\n{logged}"
    )
    assert str(outside) not in logged, (
        f"the log named the out-of-tree directory.\n\n{logged}"
    )


# ---------------------------------------------------------------------------
# 3. The redact WRITE path REFUSES
# ---------------------------------------------------------------------------


def test_redact_apply_refuses_a_source_tree_it_could_not_fully_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED. ``--apply`` destroys bytes; it may not proceed over an unread region.

    The inverse of the ingest ruling, and deliberately so. This guard is
    reached only from the interactive ``creek redact --apply`` handler — the
    pipeline's redaction pass goes through ``scan_batch`` instead — so
    refusing here cannot break ``creek sync``. What it *can* prevent is a
    safety tool printing "Applied redactions to N file(s)" over a subtree it
    never examined. That banner is a false assurance the operator's next
    action depends on, which is strictly worse than a refusal.

    Exit code 1 and a message naming the directory, matching the shape of the
    escaping-link refusal already beside it.

    ``monkeypatch.chdir`` is not cosmetic: ``--apply`` without ``--vault``
    appends to ``./00-Creek-Meta/audit/redact.jsonl``, so at HEAD — where the
    command wrongly proceeds — this test would otherwise write a tamper-evident
    audit record into the repository working tree on every red run.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture, used to move the working
            directory so the audit log lands in ``tmp_path``.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    monkeypatch.chdir(tmp_path)
    src, locked = _plant_locked_escape(tmp_path)
    (src / "leak.md").write_text(
        "Contact: alice@example.com\nSSN: 123-45-6789\n",
        encoding="utf-8",
    )
    make_unlistable(locked)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(src), "--yes"],
    )

    assert result.exit_code == 1, (
        "`redact --apply` proceeded over a source tree containing a subtree "
        "it could not list, and reported success. The operator now believes "
        "a region was scrubbed that was never opened.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert locked.name in result.output, (
        "the refusal does not name the directory that caused it, so the "
        f"operator cannot fix it.\n\n{result.output}"
    )
    assert _OUT_OF_TREE_SENTINEL not in result.output, (
        f"the refusal leaked out-of-tree content.\n\n{result.output}"
    )


def test_redact_review_refuses_a_vault_tree_it_could_not_fully_list(
    tmp_path: Path,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED. The second call site of the same guard, pinned separately.

    ``_assert_no_escaping_symlinks`` is reached from ``run_apply`` with
    ``label="source"`` and from ``run_review`` with ``label="vault"``. A fix
    applied to one call site and not the other is the exact half-fix #1294's
    review caught on the CLI ordering guard, so both arms carry their own
    behavioural test rather than sharing one.

    Args:
        tmp_path: Pytest-provided temporary directory.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    vault, locked = _plant_locked_escape(tmp_path)
    (vault / "good.md").write_text(
        "Contact: alice@example.com\n",
        encoding="utf-8",
    )
    make_unlistable(locked)

    result = runner.invoke(app, ["redact", "--review", "--vault", str(vault)])

    assert result.exit_code == 1, (
        "`redact --review` walked a vault tree it could not fully list and "
        "carried on into the write path.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert locked.name in result.output, (
        f"the refusal does not name the unlistable directory.\n\n{result.output}"
    )


# ---------------------------------------------------------------------------
# 4. The redact READ path REPORTS
# ---------------------------------------------------------------------------


def test_redact_scan_warns_about_a_subtree_it_could_not_list_and_does_not_refuse(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    make_unlistable: Callable[[Path], Path],
) -> None:
    """RED on the warning. ``--scan`` writes nothing, so it skips and says so.

    Two halves, and both are load-bearing.

    ``exit_code == 0`` pins that the read path is not turned into a hard
    failure. ``Pipeline._run_redaction`` reaches ``scan_batch`` on the
    ``creek process`` / ``creek sync`` path, so refusing here *would* hit the
    unattended loop — the argument that does not apply to ``--apply`` applies
    in full force to this surface.

    The WARNING pins that the omission is reported. ``_scannable_candidates``
    enumerates with ``dir_path.rglob("*")``, which — measured — returned only
    the locked directory and never the escaping link inside it. The scan's
    ``escaped`` counter therefore under-reports and the operator reads a clean
    result over a region that was never opened.

    The in-root control's finding is asserted present: without it, a scan that
    read nothing at all would satisfy the exit-code half perfectly.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log-capture fixture.
        make_unlistable: Fixture locking a directory with restoring teardown.
    """
    src, locked = _plant_locked_escape(tmp_path)
    (src / "notes.md").write_text("Contact: alice@example.com\n", encoding="utf-8")
    make_unlistable(locked)

    with caplog.at_level(logging.WARNING, logger="creek.redact.scanner"):
        result = runner.invoke(app, ["redact", "--scan", "--source", str(src)])

    assert result.exit_code == 0, (
        "`redact --scan` refused a tree over one unlistable subtree. The read "
        "path writes nothing and is reached by the unattended pipeline pass; "
        "refusing here disables the operator's whole safety scan.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "email" in result.output.lower(), (
        "the in-root control produced no finding, so every assertion here "
        f"would pass over a scan that read nothing.\n\n{result.output}"
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(locked) in message for message in warnings), (
        "the scan silently omitted an entire subtree. rglob swallows the "
        "scandir failure, so the region is not scanned, not counted, and not "
        "reported — the operator reads 'no findings' over a tree that was "
        f"never opened.\n\nwarnings={warnings}"
    )
    assert not any(_OUT_OF_TREE_SENTINEL in message for message in warnings), (
        f"the warning leaked out-of-tree content.\n\nwarnings={warnings}"
    )


# ---------------------------------------------------------------------------
# 5. One definition, not a fourth copy
# ---------------------------------------------------------------------------


def test_the_escaping_leaf_predicate_has_exactly_one_definition() -> None:
    """RED. The expression is currently written out twice; it must not become three.

    ``child.is_symlink() and not resolves_within(child, resolved_root)`` is
    spelled out at ``creek/_containment.py:255`` and again at
    ``creek/redact/scanner.py:757``, and #1373 needs the same test a third
    time in ``creek/vault/reader.py``. #1294 already fought this fight for
    ``resolves_within`` and settled it with an identity assertion rather than
    a behavioural one, because two copies that agree today are two copies that
    disagree after the next fix lands in one of them. This is that assertion
    for the leaf predicate.

    Identity, not equality: a re-implementation that happens to behave the
    same is exactly the drift being prevented.
    """
    from creek import _containment
    from creek.redact import scanner
    from creek.vault import reader

    assert callable(getattr(_containment, "escaping_child", None)), (
        "creek._containment does not expose a shared leaf predicate, so the "
        "expression stays copied into every walk that needs it."
    )
    assert scanner.escaping_child is _containment.escaping_child, (
        "creek.redact.scanner uses its own copy of the escaping-leaf test "
        "instead of the canonical one.\n\n"
        f"{scanner.escaping_child!r}\n{_containment.escaping_child!r}"
    )
    assert reader.escaping_child is _containment.escaping_child, (
        "creek.vault.reader defines a third copy of the escaping-leaf test. "
        "The vault loader, the scanner walk and the ingest gate must share "
        "one definition of 'this leaf leaves the root', or a future fix to "
        f"one silently leaves the others behind.\n\n{reader.escaping_child!r}"
    )
