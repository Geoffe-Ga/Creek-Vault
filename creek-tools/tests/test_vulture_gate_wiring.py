"""Every vulture caller must route through the one wrapper (issue #1395).

``test_vulture_gate.py`` proves the *policy* can fail on dead code. This
module proves the policy is actually reachable, and that it is the only
policy there is.

Both halves of #1395 were wiring defects, not analysis defects:

* the two vulture invocations that existed -- a pre-commit hook and
  ``scripts/lint-extended.sh`` -- ran in neither CI nor ``check-all.sh``,
  so the check never executed on the merge path at all; and
* each spelled its own ``--min-confidence 80``, a threshold that excludes
  the entire 60%-confidence dead-symbol tier, so even when it did run it
  could not fail.

Two call sites with two copies of a threshold is how a gate drifts back.
So the assertions below are deliberately *exclusive*: not merely "the
wrapper is called", but "nothing anywhere runs the vulture binary
directly, and no ``--min-confidence`` survives in any script, workflow,
pre-commit hook or config table". A behavioural test cannot catch a
partial revert that re-adds one raw invocation next to the wrapper; this
one can.

Parsing helpers come from ``tests/shell_command_support.py``, which is
comment-blind on purpose: these files habitually quote the very command
they run inside an explanatory comment, and prose naming a flag must
never satisfy an assertion about the flag.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

from tests.shell_command_support import (
    CI_WORKFLOW,
    CREEK_TOOLS_DIR,
    PRE_COMMIT_CONFIG,
    SCRIPTS_DIR,
    all_workflow_steps,
    command_lines,
    load_yaml,
    non_comment_lines,
    step_run_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

_WRAPPER = "lint-vulture.sh"
_WRAPPER_COMMAND = "./scripts/lint-vulture.sh"
_THRESHOLD_FLAG = "--min-confidence"
_STATIC_ANALYSIS_JOB = "Static Analysis"

# A command line that runs the vulture binary itself, however it is
# spelled: ``vulture …``, ``/usr/bin/vulture …``, ``uv run vulture …`` or
# ``python -m vulture …``. Anchored, so a line that merely mentions the
# word -- ``run_check "Dead code (vulture)" …`` -- is not a match.
_DIRECT_INVOCATION = re.compile(
    r"^(?:uv\s+run\s+|python[0-9.]*\s+-m\s+)?(?:\S*/)?vulture(?:\s|$)"
)


def _script_lines() -> list[tuple[Path, str]]:
    """Return every non-comment line of every gate script.

    Returns:
        ``(script_path, stripped_line)`` pairs, in filename order.
    """
    lines: list[tuple[Path, str]] = []
    for script in sorted(SCRIPTS_DIR.glob("*.sh")):
        lines.extend((script, line.strip()) for line in non_comment_lines(script))
    return lines


def _workflow_run_lines() -> list[str]:
    """Return every non-comment shell line in every workflow ``run`` block.

    Returns:
        The stripped command lines across all workflows.
    """
    lines = step_run_lines(all_workflow_steps(), r".")
    return [line for line in lines if not line.startswith("#")]


def _pre_commit_hooks() -> list[tuple[str, dict[str, Any]]]:
    """Return every pre-commit hook with the repo it comes from.

    Returns:
        ``(repo, hook)`` pairs from ``creek-tools/.pre-commit-config.yaml``.
    """
    config = load_yaml(PRE_COMMIT_CONFIG)
    pairs: list[tuple[str, dict[str, Any]]] = []
    for repo in config["repos"]:
        origin = str(repo.get("repo", ""))
        for hook in repo.get("hooks") or []:
            if isinstance(hook, dict):
                pairs.append((origin, hook))
    return pairs


def _static_analysis_job() -> dict[str, Any]:
    """Return the ``Static Analysis`` job of the root CI workflow.

    Returns:
        The job mapping from ``.github/workflows/ci.yml``.
    """
    jobs = load_yaml(CI_WORKFLOW)["jobs"]
    matching = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and job.get("name") == _STATIC_ANALYSIS_JOB
    ]
    assert len(matching) == 1, (
        f"expected exactly one {_STATIC_ANALYSIS_JOB!r} job in ci.yml, found "
        f"{len(matching)}; the assertions about it would be vacuous"
    )
    return matching[0]


class TestVultureGateWiring:
    """The dead-code gate runs on the merge path, from a single definition."""

    def test_check_all_runs_the_vulture_wrapper(self) -> None:
        """``check-all.sh`` must run the gate as one of its checks (#1395).

        ``check-all.sh`` is Gate 2 -- the command a developer runs before
        every commit and the one the house rules point at. A dead-code
        policy nothing on that path invokes is documentation, not a gate,
        which is exactly what the old ``lint-extended.sh``-only wiring was.
        """
        run_checks = command_lines(SCRIPTS_DIR / "check-all.sh", r"^run_check\s")
        routed = [line for line in run_checks if _WRAPPER in line]

        assert len(routed) == 1, (
            f"check-all.sh must invoke {_WRAPPER} exactly once via run_check; "
            f"its run_check lines are {run_checks!r}"
        )

    def test_check_all_help_lists_the_vulture_gate(self) -> None:
        """``--help``'s numbered gate list must name the dead-code gate.

        The heredoc in ``check-all.sh --help`` is what a developer reads to
        learn what the gate covers, and it claims to match the CI workflow.
        A gate that runs but is not listed teaches the next person that the
        list is untrustworthy; the count assertion keeps the two in step, so
        adding a check without documenting it fails here.
        """
        check_all = SCRIPTS_DIR / "check-all.sh"
        numbered = command_lines(check_all, r"^\s*[0-9]+\.\s")
        run_checks = command_lines(check_all, r"^run_check\s")
        named = [line for line in numbered if "vulture" in line.lower()]

        assert len(named) == 1, (
            "the numbered gate list in check-all.sh --help must name the "
            f"vulture gate exactly once; the list is {numbered!r}"
        )
        assert len(numbered) == len(run_checks), (
            f"check-all.sh --help lists {len(numbered)} gates but the script "
            f"runs {len(run_checks)}; the help text has drifted from the gates"
        )

    def test_ci_static_analysis_runs_the_vulture_wrapper(self) -> None:
        """CI must run the gate, from the ``creek-tools`` working directory.

        Pre-commit hooks are advisory -- CI runs no pre-commit step, and
        ``--no-verify`` exists -- so before #1395 vulture never ran on the
        merge path at all. The working-directory assertion is not
        decoration: the step invokes a *relative* path, so a job that ran
        from the repository root would fail to find the wrapper.
        """
        job = _static_analysis_job()
        steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
        run_lines = step_run_lines(steps, re.escape(_WRAPPER))
        working_directory = ((job.get("defaults") or {}).get("run") or {}).get(
            "working-directory"
        )

        assert run_lines == [_WRAPPER_COMMAND], (
            f"the {_STATIC_ANALYSIS_JOB!r} job must run {_WRAPPER_COMMAND!r} "
            f"exactly once; it runs {run_lines!r}"
        )
        assert working_directory == "creek-tools", (
            f"{_WRAPPER_COMMAND!r} is a relative path, so the job must default "
            f"to the creek-tools working directory; got {working_directory!r}"
        )

    def test_pre_commit_routes_through_the_wrapper(self) -> None:
        """The pre-commit hook must call the wrapper, not vulture upstream.

        The upstream ``jendrikseipp/vulture`` hook carried both halves of
        the defect: its own ``--min-confidence`` and its own pinned vulture
        version (2.10, while ``uv.lock`` resolves 2.16), so the hook and the
        gate could disagree about the same tree. One wrapper means one
        policy and one version.
        """
        hooks = _pre_commit_hooks()
        routed = [hook for _, hook in hooks if _WRAPPER in str(hook.get("entry", ""))]
        upstream = sorted({repo for repo, _ in hooks if "vulture" in repo.lower()})

        assert len(routed) == 1, (
            f"exactly one pre-commit hook must run {_WRAPPER}; found "
            f"{[hook.get('id') for hook in routed]!r}"
        )
        assert upstream == [], (
            "no pre-commit hook may install vulture from upstream and run it "
            f"with its own threshold and version; found {upstream!r}"
        )

    def test_lint_extended_routes_through_the_wrapper(self) -> None:
        """The extended lint suite must reuse the wrapper, not re-spell it.

        ``lint-extended.sh`` is the ad-hoc triage script; it held the second
        copy of ``--min-confidence 80``. Two spellings of one policy is the
        drift this module exists to prevent, whichever copy is the one a
        developer happens to run.
        """
        routed = command_lines(SCRIPTS_DIR / "lint-extended.sh", re.escape(_WRAPPER))

        assert len(routed) == 1, (
            f"lint-extended.sh must invoke {_WRAPPER} exactly once; found {routed!r}"
        )

    def test_the_direct_invocation_detector_actually_detects(self) -> None:
        """The detector below must match a raw call and spare a mention.

        The next two tests are only as good as this regex: one that had
        rotted into something matching nothing would report zero offenders
        forever -- a check that cannot fail, which is the whole subject of
        #1395. So it is exercised against the exact line this issue
        deleted, and against a line that merely names the tool.
        """
        deleted_line = "vulture creek/ --min-confidence 80"
        mention = 'run_check "Dead code (vulture)" "lint-vulture.sh"'

        assert _DIRECT_INVOCATION.search(deleted_line), (
            f"the detector no longer recognises a raw invocation: {deleted_line!r}"
        )
        assert _DIRECT_INVOCATION.search("uv run vulture creek creek_mcp"), (
            "the detector must see through `uv run`"
        )
        assert not _DIRECT_INVOCATION.search(mention), (
            f"the detector fires on a line that merely names vulture: {mention!r}"
        )

    def test_no_raw_vulture_invocation_survives_anywhere(self) -> None:
        """Only the wrapper may run the vulture binary (#1395).

        A second, direct invocation would come with its own scan scope and
        its own thresholds, and would answer a different question than the
        gate does -- the same way ``lint-extended.sh`` and the pre-commit
        hook once disagreed with the (nonexistent) CI check. The wrapper
        itself is included in this sweep: it runs the policy module, not
        the binary.
        """
        offenders = [
            f"{script.name}: {line}"
            for script, line in _script_lines()
            if _DIRECT_INVOCATION.search(line)
        ]
        offenders += [
            f"workflow: {line}"
            for line in _workflow_run_lines()
            if _DIRECT_INVOCATION.search(line)
        ]

        assert offenders == [], (
            "every vulture caller must route through "
            f"scripts/{_WRAPPER} so the policy cannot drift; these run the "
            f"binary directly: {offenders!r}"
        )

    def test_no_min_confidence_threshold_survives_anywhere(self) -> None:
        """No caller or config may set a single confidence threshold (#1395).

        ``--min-confidence`` is the flag that made the old check vacuous,
        and no single value for it can work: at 80 the entire unused
        function/method/class tier is invisible, and at 60 the tree reports
        hundreds of low-value variable findings. The policy is per-type, so
        the flag has no correct value anywhere -- including in a
        ``[tool.vulture]`` table, which a bare invocation would silently
        pick up.
        """
        offenders = [
            f"{script.name}: {line}"
            for script, line in _script_lines()
            if _THRESHOLD_FLAG in line
        ]
        offenders += [
            f"workflow: {line}"
            for line in _workflow_run_lines()
            if _THRESHOLD_FLAG in line
        ]
        for repo, hook in _pre_commit_hooks():
            spelled = [str(arg) for arg in (hook.get("args") or [])]
            spelled.append(str(hook.get("entry", "")))
            if any(_THRESHOLD_FLAG in item for item in spelled):
                offenders.append(f"pre-commit {repo} {hook.get('id')}: {spelled!r}")

        pyproject = tomllib.loads(
            (CREEK_TOOLS_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )
        vulture_table = pyproject.get("tool", {}).get("vulture", {})

        assert offenders == [], (
            f"{_THRESHOLD_FLAG} is a single number, and the gate's policy is "
            "per-type; these callers reintroduce it and would reopen #1395: "
            f"{offenders!r}"
        )
        assert "min_confidence" not in vulture_table, (
            "a [tool.vulture] min_confidence in pyproject.toml is picked up by "
            f"any bare vulture run; the table is {vulture_table!r}"
        )
