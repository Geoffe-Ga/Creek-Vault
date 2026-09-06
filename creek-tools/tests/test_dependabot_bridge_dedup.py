"""The Dependabot bridge must not re-file an issue it already filed.

Issue #1439. ``.github/workflows/dependabot-to-ralph-issue.yml`` guards against
double-filing with two checks, and both failed together on PRs #1162 and #1165:

* ``body_links_issue`` reads the **Dependabot-owned PR body**, and Dependabot
  rewrites that body on every rebase, erasing the ``Closes`` line the bridge
  appended (#1019).
* ``issue_exists_for_pr`` searched only issues labelled ``dependencies`` -- but
  the grooming pass replaces that label with a priority one, at which point the
  marker-carrying issue is invisible to its own dedup.

So the reconciler filed a second issue for a PR it had already bridged, and the
surviving ``Closes`` link pointed at the *duplicate*, orphaning the original.
One duplicate per PR per week, unbounded.

**Why these tests run the shell instead of reading it.** A static assertion that
``--label dependencies`` is absent pins today's spelling and nothing else; it
cannot tell you the guard actually finds a label-less issue. So the helper
region between the workflow's sentinels is extracted and sourced in a real bash
process against a stub ``gh``.

**The stub models ``gh``'s label filtering on purpose, and that is what makes
these tests non-vacuous.** A stub that ignored ``--label`` would hand the same
fixture to the fixed and unfixed code alike, and every assertion here would pass
against the bug. Because the stub honours it, the pre-#1439 call -- which passed
``--label dependencies`` -- receives an empty list for a label-less issue and
:func:`test_the_dedup_finds_a_marker_on_an_unlabelled_issue` fails. Verified by
reverting the workflow before this file was committed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.shell_command_support import WORKFLOWS_DIR, load_yaml

if TYPE_CHECKING:
    from pathlib import Path

BRIDGE_WORKFLOW = WORKFLOWS_DIR / "dependabot-to-ralph-issue.yml"

_BEGIN = "# ---8<--- BEGIN bridge helpers"
_END = "# ---8<--- END bridge helpers"

#: The PR number every fixture below is keyed on.
_PR = 1165

#: The marker the bridge writes into each issue body it files.
_MARKER = f"<!-- dependabot-pr:{_PR} -->"


def _bridge_run_script() -> str:
    """Return the ``run:`` script of the step defining the bridge helpers.

    Returns:
        The step's shell script.

    Raises:
        AssertionError: If the workflow defines no such step. Without this the
            extraction below would yield an empty region and every behavioural
            assertion would pass against nothing.
    """
    workflow: dict[str, Any] = load_yaml(BRIDGE_WORKFLOW)
    scripts = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if _BEGIN in step.get("run", "")
    ]
    assert len(scripts) == 1, (
        f"expected exactly one step in {BRIDGE_WORKFLOW.name} carrying the "
        f"{_BEGIN!r} sentinel, found {len(scripts)}. The tests below source "
        "that region; without it they would assert against an empty script."
    )
    return scripts[0]


def _helper_region() -> str:
    """Return the sentinel-delimited, side-effect-free helper definitions.

    Returns:
        The shell source between the workflow's two sentinels.

    Raises:
        AssertionError: If the region is missing, empty, or does not define the
            three helpers these tests exercise.
    """
    script = _bridge_run_script()
    assert _END in script, f"{BRIDGE_WORKFLOW.name} has no {_END!r} sentinel"
    after = script.split(_BEGIN, maxsplit=1)[1]
    # Drop the remainder of the sentinel's own line ("… keep pure) ---"), which
    # is prose, not shell: leaving it in makes line 1 a syntax error, bash
    # sources nothing, and every "expect non-zero" assertion below passes
    # against a function that was never defined.
    region = after.split("\n", maxsplit=1)[1].split(_END, maxsplit=1)[0]
    for helper in ("marker_for()", "body_links_issue()", "issue_exists_for_pr()"):
        assert helper in region, (
            f"the extracted helper region does not define {helper}; it may "
            "have been moved outside the sentinels, which would make every "
            "behavioural test below vacuous"
        )
    return region


def _write_stub_gh(directory: Path, issues: list[dict[str, Any]]) -> None:
    """Install a stub ``gh`` that mimics ``issue list``'s label filtering.

    The filtering is the load-bearing part. ``gh issue list --label X`` returns
    only issues carrying ``X``; a stub that ignored the flag would answer
    identically for the fixed and unfixed workflow, and the tests that depend
    on the difference would prove nothing.

    Args:
        directory: Directory to place the stub in; goes on ``PATH``.
        issues: Issue records, each with ``number``, ``body`` and ``labels``.
    """
    fixture = directory / "issues.json"
    fixture.write_text(json.dumps(issues), encoding="utf-8")
    stub = directory / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Stub gh: honours --label so the dedup's own scope is observable.\n"
        "label=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--label" ]]; then label="$2"; shift 2; else shift; fi\n'
        "done\n"
        f'jq --arg l "$label" \'[.[] | select($l == "" or (.labels | index($l)))]\''
        f" {fixture}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _call_dedup(
    tmp_path: Path, issues: list[dict[str, Any]], *, limit: int = 500
) -> subprocess.CompletedProcess[str]:
    """Source the helper region and run ``issue_exists_for_pr`` against a stub.

    Args:
        tmp_path: pytest temporary directory for the stub and fixture.
        issues: The issue records the stub ``gh`` will return.
        limit: Value for ``ISSUE_SCAN_LIMIT``, the scan window.

    Returns:
        The completed bash process. Exit 0 means "an issue already exists".
    """
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_helper_region(), encoding="utf-8")
    _write_stub_gh(tmp_path, issues)

    environment = dict(os.environ)
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment['PATH']}"
    environment["REPO"] = "owner/repo"
    environment["ISSUE_SCAN_LIMIT"] = str(limit)

    return subprocess.run(
        ["bash", "-c", f'source "{helpers}"; issue_exists_for_pr {_PR}'],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=str(tmp_path),
    )


def _assert_the_helper_actually_ran(
    result: subprocess.CompletedProcess[str],
) -> None:
    """Fail if bash never defined the function, rather than reading its exit.

    Every "the dedup reports absence" assertion checks for a non-zero exit --
    and bash also exits non-zero when the sourced file has a syntax error or
    the function does not exist. Those two look identical from the return code
    alone, so a broken extraction would turn each of those tests green against
    nothing. This separates them. It is not hypothetical: the first run of this
    module passed two tests exactly that way.

    Args:
        result: The completed bash process.

    Raises:
        AssertionError: If bash reported a syntax error or a missing command.
    """
    for signature in ("syntax error", "command not found", "No such file"):
        assert signature not in result.stderr, (
            f"bash never ran the helper ({signature!r} in stderr), so this "
            f"test's exit-code assertion would prove nothing: {result.stderr!r}"
        )


@pytest.fixture(autouse=True)
def _require_jq() -> None:
    """Skip loudly if ``jq`` is absent rather than passing silently.

    The stub and the workflow both use ``jq``. A machine without it would
    otherwise turn every test here into an error whose cause is not obvious.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the bridge helpers require it")


def test_the_workflow_still_exposes_its_helper_region() -> None:
    """The sentinels must exist, or every behavioural test is vacuous.

    This is the loud precondition. Delete the sentinels and the extraction
    yields nothing; without this assertion the suite would go green because it
    found no code to disagree with.
    """
    region = _helper_region()
    assert region.strip(), "the extracted helper region is empty"
    assert BRIDGE_WORKFLOW.is_file()


def test_the_dedup_finds_a_marker_on_an_unlabelled_issue(tmp_path: Path) -> None:
    """The #1439 defect, stated directly.

    An issue whose ``dependencies`` label was stripped by the grooming pass
    still carries the marker in its body. The dedup must find it. Before the
    fix the scan passed ``--label dependencies``, the stub returned nothing,
    and the reconciler filed a duplicate.
    """
    result = _call_dedup(
        tmp_path,
        [{"number": 1166, "body": f"{_MARKER}\nbody text", "labels": ["P3"]}],
    )
    assert result.returncode == 0, (
        "the dedup did not find an existing issue carrying this PR's marker "
        "because that issue is not labelled `dependencies` — so the bridge "
        f"would file a duplicate. stderr: {result.stderr!r}"
    )


def test_the_dedup_still_finds_a_labelled_issue(tmp_path: Path) -> None:
    """Widening the scan must not lose the case that already worked."""
    result = _call_dedup(
        tmp_path,
        [{"number": 864, "body": _MARKER, "labels": ["P3", "dependencies"]}],
    )
    assert result.returncode == 0, (
        f"the dedup missed a correctly-labelled issue: {result.stderr!r}"
    )


def test_the_dedup_reports_absence_so_a_genuine_bump_gets_filed(
    tmp_path: Path,
) -> None:
    """A false positive here means an issue that is never filed at all.

    This is why ``gh search`` is not used for the scan: it token-matches, so it
    returns issues that merely quote a marker -- #1439 itself is one.
    """
    result = _call_dedup(
        tmp_path,
        [{"number": 7, "body": "an issue about something else", "labels": []}],
    )
    _assert_the_helper_actually_ran(result)
    assert result.returncode != 0, (
        "the dedup claimed an issue exists for a PR that has none, so the "
        "bump would never be bridged"
    )


def test_a_shorter_pr_number_does_not_satisfy_a_longer_one(tmp_path: Path) -> None:
    """``dependabot-pr:116`` must not match ``dependabot-pr:1165``.

    The marker's trailing ``-->`` is what makes the comparison exact. A prefix
    match would silently suppress the bridge for every PR whose number extends
    an already-bridged one.
    """
    result = _call_dedup(
        tmp_path,
        [{"number": 9, "body": "<!-- dependabot-pr:116 -->", "labels": []}],
    )
    _assert_the_helper_actually_ran(result)
    assert result.returncode != 0, (
        "a shorter PR number's marker satisfied the dedup for a longer one"
    )


def test_a_truncated_scan_aborts_instead_of_answering(tmp_path: Path) -> None:
    """An unprovable "not found" must fail the run, not mint a duplicate.

    ``gh issue list --limit N`` returns at most N issues. If the repository has
    more open issues than the window, absence is unproven -- and answering
    "absent" is precisely how the original bug produced duplicates. So the
    guard refuses rather than guessing.
    """
    result = _call_dedup(
        tmp_path,
        [
            {"number": 1, "body": "one", "labels": []},
            {"number": 2, "body": "two", "labels": []},
        ],
        limit=2,
    )
    _assert_the_helper_actually_ran(result)
    assert result.returncode != 0, "a truncated scan returned an answer"
    assert "::error::" in result.stderr, (
        f"the truncated scan failed without an annotation: {result.stderr!r}"
    )
    assert "ISSUE_SCAN_LIMIT" in result.stderr, (
        "the error does not name the knob that fixes it, so the reader has no "
        f"action to take: {result.stderr!r}"
    )


def test_the_scan_window_exceeds_the_repositorys_open_issue_count() -> None:
    """The configured window must have real headroom over the backlog.

    The truncation guard turns an overrun into a failed workflow run rather
    than a duplicate, which is the right failure -- but it is still a failure.
    This keeps the value honest as the backlog grows.
    """
    script = _bridge_run_script()
    assignments = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("ISSUE_SCAN_LIMIT=")
    ]
    assert len(assignments) == 1, (
        f"expected exactly one ISSUE_SCAN_LIMIT assignment, got {assignments!r}"
    )
    limit = int(assignments[0].split("=", maxsplit=1)[1])
    assert limit >= 500, (
        f"ISSUE_SCAN_LIMIT is {limit}; the repository had 240 open issues when "
        "#1439 was fixed, and a window without headroom turns ordinary backlog "
        "growth into a failed bridge run"
    )


def test_the_dedup_scan_is_not_scoped_to_a_label() -> None:
    """Pin the fix statically as well, against a partial revert.

    The behavioural tests above would also catch this, but only while the stub
    keeps modelling ``--label``. This assertion holds regardless.
    """
    region = _helper_region()
    scan = [
        line
        for line in region.splitlines()
        if "gh issue list" in line and not line.strip().startswith("#")
    ]
    assert scan, "the dedup no longer runs `gh issue list` at all"
    joined = " ".join(scan)
    assert "--label" not in joined, (
        "the marker dedup filters `gh issue list` by label again. The grooming "
        "pass strips `dependencies`, which is exactly how #1439's duplicates "
        f"were minted. Found: {scan!r}"
    )


def test_the_pr_body_guard_is_documented_as_unreliable() -> None:
    """``body_links_issue`` must not be presented as a guarantee.

    Dependabot owns that body and erases the appended ``Closes`` line on
    rebase (#1019). A future reader who believes it is authoritative could
    reasonably re-narrow the marker scan, restoring the compound failure.
    """
    region = _helper_region()
    assert "body_links_issue()" in region
    assert "rebase" in region, (
        "the comment on body_links_issue no longer records that Dependabot "
        "rewrites the PR body on rebase, which is why the marker scan has to "
        "stand alone"
    )
