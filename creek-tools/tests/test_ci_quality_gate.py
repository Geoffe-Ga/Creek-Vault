"""The ``quality-gate`` job must actually gate every job it waits for.

``quality-gate`` carries ``if: always()``. That is deliberate — without it,
GitHub skips the job when an upstream job fails, and a *skipped* required
check is not a *failed* one, so a red matrix could report a green rollup.
But it has a sharp edge: once ``always()`` is set, ``needs:`` no longer
gates anything at all. It only orders the jobs and makes
``needs.<id>.result`` readable. The gate is the shell script, and nothing
else.

That makes two mistakes silent and identical-looking:

* a job in ``needs:`` with no matching ``!= "success"`` check — the gate
  waits for it, then passes whatever it reported;
* a job in the workflow that is missing from ``needs:`` entirely — it never
  even delays the rollup.

Either one leaves the repository ungated while every badge stays green.
#1026 established the convention and wrote the rule into a comment; this
module is what enforces it, and #1141 extended it when the static analysis
was split into its own jobs.

The second suite here is an anti-weakening ratchet for #1141 specifically.
That change was a *speed* change, and the cheapest way to make CI fast is
to stop checking things — so every gate command it moved between jobs is
pinned by name. Moving a check is fine; losing one is not.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.shell_command_support import CI_WORKFLOW, load_yaml

# Matches `needs.<job-id>.result` inside a `${{ }}` interpolation, which is
# how the gate script reads each upstream job's outcome.
_NEEDS_RESULT_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")

# Every command that must still gate a merge somewhere in ci.yml, mapped to a
# human name for the failure message. Spelled as regexes over the workflow's
# `run:` blocks so a step can move between jobs freely — the contract is that
# the check runs, not where.
_REQUIRED_GATES: dict[str, str] = {
    r"ruff check \.": "ruff lint",
    r"ruff format --check": "ruff format check",
    r"interrogate ": "docstring coverage",
    r"lint-tryceratops\.sh": "tryceratops exception hygiene",
    r"bandit -r creek/ creek_mcp/ -ll": "bandit medium-severity gate",
    r"lint-refurb\.sh": "refurb modernisation gate",
    r"pylint\.sh": "pylint score gate",
    r"typecheck\.sh": "mypy strict",
    r"pip-audit ": "dependency vulnerability audit",
    r"test\.sh --unit --coverage": "unit suite with coverage",
    r"test\.sh --integration": "hermetic integration lane",
    r"test\.sh --e2e": "hermetic e2e lane",
    r"coverage report --fail-under": "aggregate coverage gate",
    r"coverage-per-file\.sh": "per-file coverage gate",
    r"complexity\.sh": "cyclomatic complexity gate",
    # Spelled as the gate branch itself, not the bare word "crawdad": #1501
    # puts a crawdad-scoped temp path (`/tmp/crawdad-locked-requirements.txt`)
    # into the crawdad job's `run:` block, so a loose `crawdad` would be
    # satisfied by that filename and keep passing after someone deleted the
    # gate script's crawdad branch — a guard green on the wrong line.
    r"needs\.crawdad\.result": "crawdad job gated by quality-gate",
}

_GATE_JOB = "quality-gate"


def _workflow() -> dict[str, object]:
    """Return the parsed CI workflow document."""
    return load_yaml(CI_WORKFLOW)


def _jobs() -> dict[str, dict[str, object]]:
    """Return the workflow's job mappings, keyed by job id."""
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict), "ci.yml has no `jobs:` mapping"
    return jobs


def _gate_job() -> dict[str, object]:
    """Return the ``quality-gate`` job mapping."""
    jobs = _jobs()
    assert _GATE_JOB in jobs, f"ci.yml no longer defines a `{_GATE_JOB}` job"
    return jobs[_GATE_JOB]


def _needed_jobs() -> set[str]:
    """Return the job ids listed in ``quality-gate``'s ``needs:``."""
    needs = _gate_job()["needs"]
    if isinstance(needs, str):
        return {needs}
    assert isinstance(needs, list), f"`{_GATE_JOB}` has a malformed `needs:`"
    return {str(item) for item in needs}


def _checked_jobs() -> set[str]:
    """Return the job ids the gate script actually tests for success."""
    steps = _gate_job()["steps"]
    assert isinstance(steps, list), f"`{_GATE_JOB}` has no steps"
    checked: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        run_block = step.get("run")
        if not isinstance(run_block, str):
            continue
        for line in run_block.splitlines():
            if '!= "success"' not in line:
                continue
            checked.update(_NEEDS_RESULT_RE.findall(line))
    return checked


def _all_run_text() -> str:
    """Return every ``run:`` block in the workflow, concatenated.

    Parsing the YAML (rather than scanning the raw file) is what drops the
    comments, several of which quote the very command a step runs.
    """
    blocks: list[str] = []
    for job in _jobs().values():
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        blocks.extend(
            str(step["run"])
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        )
    return "\n".join(blocks)


def test_quality_gate_runs_even_when_upstream_fails() -> None:
    """The gate must keep ``if: always()``, or a red matrix skips it green."""
    condition = str(_gate_job().get("if", "")).strip()
    assert "always()" in condition, (
        f"`{_GATE_JOB}` lost `if: always()` (got {condition!r}); without it a "
        "failing upstream job SKIPS the gate, and a skipped required check "
        "does not block a merge"
    )


def test_every_needed_job_is_explicitly_checked() -> None:
    """Every job in ``needs:`` must have a matching ``!= "success"`` check.

    This is the silent no-op #1026 documented: under ``always()`` a job in
    ``needs:`` that the script never inspects is waited on and then ignored.
    """
    needed = _needed_jobs()
    checked = _checked_jobs()
    unchecked = needed - checked
    assert not unchecked, (
        f"`{_GATE_JOB}` waits for {sorted(unchecked)} but never checks the "
        'result; add an explicit `!= "success"` branch or the job is a no-op'
    )


def test_every_checked_job_is_actually_awaited() -> None:
    """The gate must not check a job it does not wait for.

    ``needs.<id>.result`` for a job absent from ``needs:`` evaluates to the
    empty string, which is ``!= "success"`` — so this spelling fails the
    build unconditionally rather than gating anything.
    """
    stray = _checked_jobs() - _needed_jobs()
    assert not stray, (
        f"`{_GATE_JOB}` checks {sorted(stray)} without listing them in "
        "`needs:`; that result is always empty, so the gate can never pass"
    )


def test_every_job_in_the_workflow_reaches_the_gate() -> None:
    """No job may exist outside the gate's `needs:` list.

    A job that gates nothing is worse than no job: it burns a runner and
    reports its own red check while the aggregated rollup stays green.
    """
    orphans = set(_jobs()) - {_GATE_JOB} - _needed_jobs()
    assert not orphans, (
        f"jobs {sorted(orphans)} are defined in ci.yml but absent from "
        f"`{_GATE_JOB}`'s `needs:`, so their failures never block a merge"
    )


def test_no_gate_command_was_dropped_from_ci() -> None:
    """Every gating check must still run somewhere in the workflow.

    Issue #1141 moved checks between jobs to cut the critical path. Moving
    them is fine. Deleting one would also make CI faster, which is exactly
    why this list exists: a speed change must never be able to buy time by
    quietly shedding a gate.
    """
    run_text = _all_run_text()
    missing = sorted(
        name
        for pattern, name in _REQUIRED_GATES.items()
        if not re.search(pattern, run_text)
    )
    assert not missing, (
        f"these gates no longer run anywhere in ci.yml: {missing}. Removing a "
        "check is not a speed-up, it is a regression"
    )


def test_python_matrix_still_covers_every_supported_version() -> None:
    """The version-sensitive job must keep the full support matrix.

    Trimming a leg is the other tempting way to make CI look fast. The
    matrix is the whole reason `tests` is a separate job from the static
    analysis, so it is pinned here.
    """
    versions = {
        str(version)
        for job in _jobs().values()
        for version in _matrix_versions(job)
        if "creek-tools" in str(job.get("defaults", ""))
    }
    assert {"3.11", "3.12", "3.13"} <= versions, (
        f"creek-tools CI no longer runs the full Python matrix, got {versions}"
    )


def _matrix_versions(job: dict[str, object]) -> list[object]:
    """Return the ``python-version`` matrix entries of ``job``, if any."""
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return []
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return []
    versions = matrix.get("python-version")
    return versions if isinstance(versions, list) else []


def test_no_test_function_is_silently_uncollected() -> None:
    """No ``tests/`` function starts with ``test`` but not ``test_``.

    ``pyproject.toml``'s ``python_functions = ["test_*"]`` matches on the
    literal ``test_`` prefix, so ``testfoo`` is not collected — pytest drops
    it without a warning and the gate reports green having run one fewer
    test than it claims. That is the same failure family as an emptied
    ``parametrize`` list: coverage disappears behind a passing gate.

    This is not hypothetical. A bulk rename of ``_load_post_or_report`` to
    ``load_post_or_raise`` (#1548) ate the underscore in
    ``test_load_post_or_report_x`` — the old name begins at index 4, so the
    result was ``testload_post_or_raise_x`` — and silently voided the only
    three direct tests of that helper's contract. Local and CI runs both
    stayed green.
    """
    import re

    tests_dir = Path(__file__).parent
    pattern = re.compile(r"^\s*(?:async\s+)?def (test[^_(\s][\w]*)", re.MULTILINE)

    offenders: list[str] = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}::{name}")

    assert offenders == [], (
        "these functions look like tests but pytest will not collect them "
        f"(need a `test_` prefix): {offenders}"
    )
