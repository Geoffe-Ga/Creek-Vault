"""``scripts/test.sh`` must not let a fleet lane claim every core (#1554).

``-n auto`` gives pytest-xdist one worker per core, which is correct when the
run owns the machine — CI gives each job its own runner, and a developer
running the suite once has the box to themselves. It is wrong inside a Ralph
fleet lane: ``check-all.sh`` invokes ``test.sh``, and the fleet runs up to
``max_workers`` lanes at once, so the real worker count is ``lanes x ncpu``.
On a 10-core / 16 GB machine that was ~60 pytest processes at ~1 GB each, and
it exhausted memory.

These tests pin the arguments ``test.sh`` *constructs*, never the suite it
would run: a stub ``python`` on ``PATH`` answers the toolchain probe and
records the argv it was handed. That keeps the test cheap (no pytest inside
pytest) while still exercising the real script, the real branch and the real
lane detection rather than a re-implementation of them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# The number of concurrent lanes the script assumes when nothing says
# otherwise — fleet.sh's own documented default for max_workers.
ASSUMED_CONCURRENT_LANES: Final[int] = 4


def expected_lane_workers() -> int:
    """The slice one lane should get on the machine running these tests.

    Restated from the contract rather than imported from the script, so a
    change to the script's arithmetic fails here instead of silently agreeing
    with itself. Rounding down is deliberate: guessing high oversubscribes
    every lane at once, guessing low only costs time.

    Returns:
        Cores divided among the assumed lanes, floored at 1.
    """
    return max(1, (os.cpu_count() or 1) // ASSUMED_CONCURRENT_LANES)


_STUB_PYTHON: Final[str] = """#!/usr/bin/env bash
# Answers `python -c "import pytest"` (the _lib.sh toolchain probe) and records
# the argv of `python -m pytest ...` without running anything.
if [[ "$1" == "-c" ]]; then
    exit 0
fi
printf '%s\\n' "$*" > "$CAPTURED_ARGV_FILE"
exit 0
"""


def _run_test_sh(
    *,
    project_root: Path,
    tmp_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Run ``test.sh --unit`` against a stub interpreter and return its argv.

    Args:
        project_root: Directory holding the ``scripts/`` copy to run, so the
            script's own ``PROJECT_ROOT`` (derived from ``BASH_SOURCE``) sits
            under a path this test controls. That is what makes lane detection
            testable without a real fleet.
        tmp_path: pytest temporary directory for the stub and capture file.
        env_overrides: Environment entries layered over the sanitized base.

    Returns:
        The single line of argv the stub ``python`` received, or the empty
        string when the script exited before invoking it.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "python"
    stub.write_text(_STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)

    captured = tmp_path / "argv.txt"

    env = dict(os.environ)
    # CREEK_TEST_WORKERS is the variable under test: never inherit the
    # operator's or a parent lane's value, or every assertion below measures
    # the ambient environment instead of the script. CI is popped for the same
    # reason - these tests run *in* CI.
    env.pop("CREEK_TEST_WORKERS", None)
    env.pop("CI", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CAPTURED_ARGV_FILE"] = str(captured)
    env.update(env_overrides or {})

    # Fixed argv, no shell: the only variable parts are paths this test built.
    completed = subprocess.run(
        ["bash", str(project_root / "scripts" / "test.sh"), "--unit"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    if not captured.exists():
        # The script exited before reaching `python -m pytest`. Without its
        # output the caller only learns "argv was empty", which says nothing
        # about why - so hand back the diagnosis rather than swallowing it.
        msg = (
            f"test.sh exited {completed.returncode} without invoking python.\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
        raise AssertionError(msg)

    return captured.read_text(encoding="utf-8").strip()


@pytest.fixture
def script_tree(tmp_path: Path) -> Path:
    """Copy ``scripts/`` under *tmp_path* so ``PROJECT_ROOT`` can be positioned.

    ``test.sh`` derives ``PROJECT_ROOT`` from its own ``BASH_SOURCE``, so the
    only honest way to exercise lane detection is to run a real copy of the
    script from a real path. Copying rather than symlinking keeps
    ``BASH_SOURCE`` resolution inside the temporary tree.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        A project root containing ``scripts/`` and an empty ``tests/``.
    """
    root = tmp_path / "checkout" / "creek-tools"
    root.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "scripts", root / "scripts")
    (root / "tests").mkdir()
    return root


def _as_lane(script_tree: Path, tmp_path: Path) -> Path:
    """Re-root a copied checkout under a ``.ralph/worktrees/`` path.

    Args:
        script_tree: The plain copied checkout.
        tmp_path: pytest temporary directory.

    Returns:
        A project root whose path marks it as a fleet lane.
    """
    lane_root = tmp_path / "repo" / ".ralph" / "worktrees" / "issue-999" / "creek-tools"
    lane_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(script_tree, lane_root)
    return lane_root


def test_a_fleet_lane_takes_a_slice_not_the_whole_box(
    script_tree: Path, tmp_path: Path
) -> None:
    """A checkout under ``.ralph/worktrees/`` must not ask for ``-n auto``.

    This is the #1554 regression itself: every lane's Gate 2 reaches this code
    path, and each one previously claimed one worker per core.

    The assertion is written around the *contract* — "a lane never claims every
    core" — rather than around a specific number, because the number depends on
    the machine. On a box with fewer cores than assumed lanes the slice floors
    at 1, and the script's pre-existing rule is that 1 means *serial*: it omits
    ``-n`` entirely rather than passing ``-n 1``. An earlier version of this
    test asserted ``-n {slice}`` unconditionally; it passed on a 10-core
    developer machine (slice 2) and failed on CI's 4-core runner (slice 1),
    which is precisely the machine-dependence this whole issue is about.
    """
    argv = _run_test_sh(project_root=_as_lane(script_tree, tmp_path), tmp_path=tmp_path)

    assert "-n auto" not in argv, f"a lane still claimed every core: {argv}"

    slice_size = expected_lane_workers()
    if slice_size > 1:
        assert f"-n {slice_size}" in argv, argv
    else:
        # Floored to one worker, which this script spells as "no xdist".
        assert " -n " not in f" {argv} ", f"expected serial, got: {argv}"

    # A fixed count would satisfy the assertions above while still
    # oversubscribing a small box (2 workers x 4 lanes on 4 cores is the same
    # defect, only smaller). On any machine big enough for the division to
    # bite, the slice must be strictly smaller than the core count.
    cores = os.cpu_count() or 1
    if cores >= ASSUMED_CONCURRENT_LANES * 2:
        assert slice_size < cores


def test_outside_a_lane_the_whole_box_is_still_used(
    script_tree: Path, tmp_path: Path
) -> None:
    """The non-lane default stays ``-n auto`` — the speedup #1141 bought.

    Pinning both branches matters: a "fix" that made *every* run take two
    workers would pass the lane test above while quietly multiplying CI time.
    """
    argv = _run_test_sh(project_root=script_tree, tmp_path=tmp_path)

    assert "-n auto" in argv, argv


@pytest.mark.parametrize("serial_value", ["0", "1"])
def test_an_explicit_serial_request_is_still_honoured_in_a_lane(
    script_tree: Path, tmp_path: Path, serial_value: str
) -> None:
    """``CREEK_TEST_WORKERS=0/1`` still means serial, lane or not.

    xdist swallows pdb, so this is the escape hatch an interactive debugging
    session depends on. Lane detection must not override an explicit request.

    Args:
        script_tree: Copied project root.
        tmp_path: pytest temporary directory.
        serial_value: The two values the script treats as "no xdist".
    """
    argv = _run_test_sh(
        project_root=_as_lane(script_tree, tmp_path),
        tmp_path=tmp_path,
        env_overrides={"CREEK_TEST_WORKERS": serial_value},
    )

    assert " -n " not in f" {argv} ", f"serial run still passed -n: {argv}"


def test_an_explicit_budget_overrides_lane_detection(
    script_tree: Path, tmp_path: Path
) -> None:
    """An orchestrator that knows the real lane count sets the exact budget.

    Lane detection is a floor for the uninformed case, not a ceiling on the
    informed one — otherwise a wave that can afford more workers cannot ask.
    """
    argv = _run_test_sh(
        project_root=_as_lane(script_tree, tmp_path),
        tmp_path=tmp_path,
        env_overrides={"CREEK_TEST_WORKERS": "5"},
    )

    assert "-n 5" in argv, argv


def test_the_help_text_describes_the_formula_the_script_uses() -> None:
    """``--help`` states the derived slice, not a number that drifts from it.

    The first cut of this change hardcoded 2 and said "drops to 2"; the
    follow-up derived the slice and left the sentence behind, so the help was
    wrong on both machine shapes this issue exists to handle — 4-core CI and a
    12-core box. Restating a value is what let it drift, so the restatement is
    pinned to the constant the script actually uses.
    """
    script = (REPO_ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
    help_block = script.split("CREEK_TEST_WORKERS", 1)[1].split("WHICH LANE", 1)[0]

    assert f"nproc / {ASSUMED_CONCURRENT_LANES}" in help_block, help_block
    assert "drops to 2" not in help_block, "the pre-derivation wording is back"
    # A slice of 1 is spelled as "no -n at all"; the help must say so, because
    # that is the branch a 4-core CI runner actually takes.
    assert "serial" in help_block.lower(), help_block
