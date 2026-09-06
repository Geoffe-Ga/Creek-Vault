"""A missing ``dependencies`` label must warn, not fail the bridge run.

Issue #1685. ``.github/workflows/dependabot-to-ralph-issue.yml`` files a Ralph
issue for each open Dependabot PR, re-applies the ``dependencies`` label with a
dedicated ``gh issue edit --add-label`` call (the REST *create* endpoint
silently drops labels when the token lacks push/triage access), then reads the
label back. When the read-back comes up short it emits ``::error::`` and exits
non-zero — on both the event path and at the end of the reconciler batch.

The token cannot grant itself that permission, so the run goes red for a
condition the workflow has no way to remedy. Run 32881808937 is the observed
instance: the label re-apply exited 0 *silently* and the read-back returned only
``P3``. Everything the bridge exists to do had already succeeded — the issue was
filed and the PR body linked — and the red check reported none of that.

**Two things about the premise, verified at HEAD, that the issue body gets
wrong.** The headline "fails on every PR" cannot happen today: commit ebac98b
commented out both ``pull_request_target`` and ``schedule``, so only
``workflow_dispatch`` is live and the ``pull_request_target`` hard-fail branch
is unreachable dead code. The reachable hard-fail is the reconciler tail. And
the workflow comment the issue asks to correct is not the one it names — the
sentence #1682 already fixed is fine; the one still false is the claim that the
label is "how ``pick-next.sh`` sees the issue at all", falsified by
``scripts/ralph/pick-next.sh``, whose ``REQUIRE_LABELS`` defaults to empty and
is set nowhere in the tree outside its own test.

**Why these tests run the shell.** A static check that ``exit 1`` is gone pins
today's spelling and cannot tell you the run actually survives a permission gap.
So the workflow's step script is executed for real against a stub ``gh`` that
models run 32881808937 exactly — including the label re-apply succeeding
silently, which is what makes the read-back the only signal.

**The one trap this module has to defend against.** The passing assertion for
the fix is ``returncode == 0`` — byte-identical to what a failed sentinel
extraction or a bash syntax error produces. Every behavioural case therefore
runs :func:`~tests.test_dependabot_bridge_dedup._assert_the_helper_actually_ran`
before reading an exit code.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.shell_command_support import WORKFLOWS_DIR, load_yaml, non_comment_lines
from tests.test_dependabot_bridge_dedup import _assert_the_helper_actually_ran

if TYPE_CHECKING:
    from pathlib import Path

BRIDGE_WORKFLOW = WORKFLOWS_DIR / "dependabot-to-ralph-issue.yml"

_BEGIN = "# ---8<--- BEGIN bridge helpers"
_END = "# ---8<--- END bridge helpers"

#: The Dependabot PR every fixture below bridges.
_PR = 1706

#: The issue number the stub's ``gh issue create`` mints for it.
_ISSUE = 1700

#: The sentence in the workflow that ``pick-next.sh`` falsifies. Its
#: ``REQUIRE_LABELS`` defaults to empty, so an unlabelled issue is picked up
#: like any other; the label is an organising signal, not a visibility gate.
_FALSE_PICKER_CLAIM = "how `pick-next.sh` sees the issue at all"


def _bridge_run_script() -> str:
    """Return the ``run:`` script of the step that defines the bridge helpers.

    Returns:
        The step's shell script.

    Raises:
        AssertionError: If no such step exists, which would make every
            behavioural assertion below run against an empty script.
    """
    workflow: dict[str, Any] = load_yaml(BRIDGE_WORKFLOW)
    scripts: list[str] = [
        str(step["run"])
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if _BEGIN in step.get("run", "")
    ]
    assert len(scripts) == 1, (
        f"expected exactly one step in {BRIDGE_WORKFLOW.name} carrying the "
        f"{_BEGIN!r} sentinel, found {len(scripts)}"
    )
    return scripts[0]


def _helper_region() -> str:
    """Return the sentinel-delimited helper definitions, including ``bridge_pr``.

    ``bridge_pr`` is where the label re-apply, the read-back and the
    diagnose-and-die all live, and today it sits *below* the closing sentinel —
    outside anything a test can source. Requiring it here is what turns a future
    re-narrowing of the region into a loud failure instead of a silent one that
    makes :func:`test_a_label_gap_warns_instead_of_failing_the_run` vacuous.

    Returns:
        The shell source between the workflow's two sentinels.

    Raises:
        AssertionError: If the region is missing or does not define the four
            helpers these tests exercise.
    """
    script = _bridge_run_script()
    assert _END in script, f"{BRIDGE_WORKFLOW.name} has no {_END!r} sentinel"
    after = script.split(_BEGIN, maxsplit=1)[1]
    # Drop the rest of the sentinel's own line ("… keep pure) ---"), which is
    # prose: leaving it in makes line 1 a syntax error, bash defines nothing,
    # and every exit-code assertion below passes against a missing function.
    region = after.split("\n", maxsplit=1)[1].split(_END, maxsplit=1)[0]
    for helper in (
        "marker_for()",
        "body_links_issue()",
        "issue_exists_for_pr()",
        "bridge_pr()",
    ):
        assert helper in region, (
            f"the extracted helper region does not define {helper}; it sits "
            "outside the `---8<---` sentinels, so no test can source it and "
            "the label-verify behaviour is unreachable from the suite (#1685)"
        )
    return region


def _write_stub_gh(directory: Path, *, labels_after_reapply: list[str]) -> None:
    """Install a stub ``gh`` modelling failing run 32881808937.

    The load-bearing detail is that ``issue edit --add-label`` exits 0 while the
    label does not stick. That is what was actually observed, and it is why the
    read-back is the only diagnosis available: a stub whose ``--add-label``
    failed loudly would answer identically for the fixed and unfixed workflow.

    Args:
        directory: Directory to place the stub in; goes on ``PATH``.
        labels_after_reapply: What ``issue view --json labels`` reports once the
            re-apply has "succeeded". Omitting ``dependencies`` models the
            permission gap; including it models a healthy token.
    """
    pr_record = {
        "number": _PR,
        "title": "bump httpx from 0.27.0 to 0.28.1",
        "body": "Bumps httpx.\n\nno issue link here",
        "headRefName": f"dependabot/pip/httpx-{_PR}",
    }
    (directory / "pr-list.json").write_text(json.dumps([pr_record]), encoding="utf-8")
    (directory / "labels.txt").write_text(
        "\n".join(labels_after_reapply) + "\n", encoding="utf-8"
    )
    stub = directory / "gh"
    stub.write_text(
        f"""#!/usr/bin/env bash
# Stub gh modelling run 32881808937: the label re-apply succeeds SILENTLY and
# the read-back is the only place the gap is visible.
set -uo pipefail
case "$1 ${{2:-}}" in
  "pr list")   cat "{directory}/pr-list.json" ;;
  "pr view")   jq -r '.[0].body' "{directory}/pr-list.json" ;;
  "pr edit")   echo "https://github.com/owner/repo/pull/{_PR}" ;;
  "issue list") echo '[]' ;;
  "issue create") echo "https://github.com/owner/repo/issues/{_ISSUE}" ;;
  "issue edit") exit 0 ;;
  "issue view") cat "{directory}/labels.txt" ;;
  *) echo "stub gh: unhandled: $*" >&2; exit 3 ;;
esac
""",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _environment(tmp_path: Path) -> dict[str, str]:
    """Return the workflow step's environment with the stub first on ``PATH``.

    Args:
        tmp_path: Directory holding the stub ``gh``.

    Returns:
        The environment mapping to hand to :func:`subprocess.run`.
    """
    environment = dict(os.environ)
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment['PATH']}"
    environment["REPO"] = "owner/repo"
    # workflow_dispatch is the ONLY live trigger at HEAD (pull_request_target
    # and schedule are commented out), so this is the reachable path.
    environment["EVENT_NAME"] = "workflow_dispatch"
    environment["PR_NUMBER"] = str(_PR)
    environment["PR_TITLE"] = "bump httpx from 0.27.0 to 0.28.1"
    return environment


def _run_bridge_pr(
    tmp_path: Path, *, labels_after_reapply: list[str]
) -> subprocess.CompletedProcess[str]:
    """Source the helper region and bridge one PR through ``bridge_pr``.

    Args:
        tmp_path: pytest temporary directory for the stub and fixtures.
        labels_after_reapply: What the label read-back reports.

    Returns:
        The completed bash process.
    """
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_helper_region(), encoding="utf-8")
    _write_stub_gh(tmp_path, labels_after_reapply=labels_after_reapply)
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            "ISSUE_SCAN_LIMIT=500\n"
            "contract='Adopt Dependabot PR #{PR}.'\n"
            "reconcile_failures=()\n"
            f'source "{helpers}"\n'
            f'bridge_pr {_PR} "a bump"\n',
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(tmp_path),
        cwd=str(tmp_path),
    )


def _run_whole_step(
    tmp_path: Path, *, labels_after_reapply: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run the workflow step's entire ``run:`` script on the reconciler path.

    This is the only reachable hard-fail at HEAD: the ``pull_request_target``
    branch inside ``bridge_pr`` is dead code because its trigger is commented
    out, so the batch tail is what actually turns a run red.

    Args:
        tmp_path: pytest temporary directory for the stub and fixtures.
        labels_after_reapply: What the label read-back reports.

    Returns:
        The completed bash process.
    """
    script = tmp_path / "step.sh"
    script.write_text(_bridge_run_script(), encoding="utf-8")
    _write_stub_gh(tmp_path, labels_after_reapply=labels_after_reapply)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(tmp_path),
        cwd=str(tmp_path),
    )


@pytest.fixture(autouse=True)
def _require_jq() -> None:
    """Skip loudly if ``jq`` is absent rather than passing silently.

    Both the stub and the workflow use ``jq``. A machine without it would turn
    every test here into an error whose cause is not obvious.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the bridge helpers require it")


def test_the_helper_region_exposes_bridge_pr(tmp_path: Path) -> None:
    """The loud precondition: the label-verify code must be extractable.

    ``tmp_path`` is unused; it keeps the signature uniform with the behavioural
    cases so a future edit cannot accidentally drop the fixture.
    """
    assert tmp_path.is_dir()
    region = _helper_region()
    assert "--add-label dependencies" in region, (
        "the extracted region no longer contains the label re-apply, so the "
        "behaviour under test is not in scope of these tests"
    )


def test_a_label_gap_warns_instead_of_failing_the_run(tmp_path: Path) -> None:
    """The bridge did its job; a permission it cannot grant must not go red.

    The issue is filed and the PR body links it, so ``body_links_issue``
    short-circuits every re-run — no duplicate is ever minted while the label
    is added by hand. The only thing the red check buys is a blocked PR.
    """
    result = _run_bridge_pr(tmp_path, labels_after_reapply=["P3"])
    _assert_the_helper_actually_ran(result)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        "bridge_pr failed the run because the `dependencies` label did not "
        "stick, even though the issue was filed and the PR linked. The token "
        "cannot grant itself Issues/triage permission, so this is a red check "
        f"nothing in the workflow can clear (#1685). output: {combined!r}"
    )
    assert "::warning::" in combined, (
        "no `::warning::` was emitted for the missing label; downgrading the "
        "failure must not also delete the diagnosis — the operator still needs "
        f"to be told to add it by hand. output: {combined!r}"
    )
    assert "::error::" not in combined, (
        f"the label gap is still reported as an error: {combined!r}"
    )


def test_the_label_warning_names_the_label_and_the_remedy(tmp_path: Path) -> None:
    """A warning nobody can act on is the same silence, one severity down.

    The two diagnoses the workflow distinguishes today (a transient read
    failure versus a real permission gap) both end in the same one-line manual
    fix, and that line has to survive the downgrade.
    """
    result = _run_bridge_pr(tmp_path, labels_after_reapply=["P3"])
    _assert_the_helper_actually_ran(result)
    combined = result.stdout + result.stderr
    warnings = [line for line in combined.splitlines() if "::warning::" in line]
    assert warnings, f"no warning line to inspect: {combined!r}"
    text = "\n".join(warnings)
    assert "dependencies" in text, (
        f"the warning does not name the missing label: {text!r}"
    )
    assert "--add-label" in text, (
        "the warning does not carry the one-line remedy "
        f"(`gh issue edit N --add-label dependencies`): {text!r}"
    )
    assert str(_ISSUE) in text, (
        f"the warning does not name the issue that needs the label: {text!r}"
    )


def test_the_reconciler_batch_completes_instead_of_failing(tmp_path: Path) -> None:
    """The batch tail is the only hard-fail still reachable at HEAD.

    ``pull_request_target`` is commented out, so ``bridge_pr``'s own
    ``exit 1`` branch cannot run. This one can, and it is what turned run
    32881808937 red after every side effect had already succeeded.
    """
    result = _run_whole_step(tmp_path, labels_after_reapply=["P3"])
    _assert_the_helper_actually_ran(result)
    combined = result.stdout + result.stderr
    assert "Reconciler complete." in combined, (
        "the reconciler exited before its final line, so the batch did not "
        f"complete: {combined!r}"
    )
    assert result.returncode == 0, (
        "the reconciler batch failed the run over a label it cannot apply, "
        "after having already filed the issue and linked the PR (#1685). "
        f"output: {combined!r}"
    )
    assert "::warning::" in combined, (
        f"the batch swallowed the label gap entirely: {combined!r}"
    )
    assert "::error::" not in combined, (
        f"the batch still reports the label gap as an error: {combined!r}"
    )


def test_a_healthy_token_produces_no_diagnostic_at_all(tmp_path: Path) -> None:
    """The inverse, without which an unconditional warning would pass.

    Flip only the stub's read-back and the warning must disappear. A fix that
    emits ``::warning::`` on every run satisfies every assertion above and
    fails here.
    """
    result = _run_whole_step(tmp_path, labels_after_reapply=["dependencies", "P3"])
    _assert_the_helper_actually_ran(result)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"a healthy bridge run failed: {combined!r}"
    assert "::warning::" not in combined, (
        "a warning was emitted even though the label read back correctly, so "
        f"the warning is unconditional and proves nothing: {combined!r}"
    )
    assert "::error::" not in combined, (
        f"a healthy bridge run emitted an error: {combined!r}"
    )


def test_the_label_reapply_survives(tmp_path: Path) -> None:
    """Downgrading the failure must not become deleting the re-apply.

    The issue states this as a constraint: the dedicated ``--add-label`` call
    is the endpoint that errors instead of silently dropping, so it is the only
    thing that ever makes the label stick when the token *can* apply it.
    """
    assert tmp_path.is_dir()
    executable = [
        line
        for line in non_comment_lines(BRIDGE_WORKFLOW)
        if "--add-label dependencies" in line
    ]
    assert executable, (
        "no non-comment line of the bridge workflow re-applies the "
        "`dependencies` label; #1685 downgrades the failure, it does not "
        "remove the fix that makes the label stick"
    )


def test_the_workflow_no_longer_claims_the_label_gates_the_picker(
    tmp_path: Path,
) -> None:
    """The stated reason for the hard-fail is false, and must not outlive it.

    ``scripts/ralph/pick-next.sh`` reads ``REQUIRE_LABELS`` with an empty
    default, and nothing in the tree sets it outside that script's own test. An
    unlabelled issue is therefore picked up like any other, so a dropped label
    never left "a filed issue no lane will ever pick up".
    """
    assert tmp_path.is_dir()
    text = BRIDGE_WORKFLOW.read_text(encoding="utf-8")
    assert _FALSE_PICKER_CLAIM not in text, (
        f"{BRIDGE_WORKFLOW.name} still claims the label is "
        f"{_FALSE_PICKER_CLAIM!r}. pick-next.sh defaults REQUIRE_LABELS to "
        "empty, so that is false — and it is the justification the hard-fail "
        "rests on, so leaving it re-argues the bug (#1685)"
    )
