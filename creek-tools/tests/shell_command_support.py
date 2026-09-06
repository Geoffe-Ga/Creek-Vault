"""Parsing helpers for tests that assert on shell scripts and CI workflows.

Several suites pin the *shape of the quality gates themselves* rather than
the behaviour of ``creek/``: they read ``scripts/*.sh``,
``.github/workflows/*.yml`` and ``.pre-commit-config.yaml`` and assert that
the commands those files run are the ones the gate contract promises
(``test_scanner_coverage.py`` for the issue #925 scanner widening,
``test_ruff_gate_parity.py`` for the issue #1119 ruff cache-freedom).

They all need the same three primitives, and they all need them to be
comment-blind: these files habitually quote the very command they invoke
inside an explanatory comment, so a naive substring scan would assert
against prose rather than against the command that actually executes.

``pyproject.toml`` sets ``python_files = ["test_*.py"]``, so this module is
never collected as a test module -- it is a plain support module in the
same spirit as ``tests/helpers.py``.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import yaml

CREEK_TOOLS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CREEK_TOOLS_DIR.parent
SCRIPTS_DIR = CREEK_TOOLS_DIR / "scripts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
RALPH_WORKFLOW = WORKFLOWS_DIR / "ralph-recap-tests.yml"
RALPH_SCRIPTS_DIR = REPO_ROOT / "scripts" / "ralph"
PRE_COMMIT_CONFIG = CREEK_TOOLS_DIR / ".pre-commit-config.yaml"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def non_comment_lines(script: Path) -> list[str]:
    """Return the lines of ``script`` that are not shell comments.

    A line whose left-stripped form starts with ``#`` is a comment and is
    dropped, so prose that quotes a command cannot satisfy an assertion
    about the command that actually executes.

    Args:
        script: Path to the shell script to read.

    Returns:
        Every non-comment line, with original indentation preserved.
    """
    return [
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def command_lines(script: Path, pattern: str) -> list[str]:
    """Return non-comment lines of ``script`` matching ``pattern``.

    Args:
        script: Path to the shell script to read.
        pattern: Regular expression searched against each raw (still
            indented) non-comment line.

    Returns:
        The matching lines, stripped of surrounding whitespace.
    """
    regex = re.compile(pattern)
    return [line.strip() for line in non_comment_lines(script) if regex.search(line)]


def shell_tokens(command: str) -> list[str]:
    """Split a shell command line into tokens, dropping any inline comment.

    A trailing line-continuation backslash is stripped first so
    :func:`shlex.split` does not choke on a dangling escape.

    ``comments=True`` is load-bearing rather than tidiness. Callers use the
    token list to assert that a gate command carries a required flag --
    ``--no-cache`` for issue #1119, for instance. Without it, an unquoted
    ``#`` and everything after it stay in the token stream, so a line such
    as ``ruff check . --fix  # still needs --no-cache`` would satisfy a
    ``"--no-cache" in shell_tokens(line)`` assertion while the flag itself
    is gone from the command that actually runs. That is a gate check that
    cannot fail -- the exact defect class these suites exist to prevent --
    so tokenisation fails closed: prose naming a flag is never the flag.
    :func:`shlex.split` stays quote-aware, so a ``#`` inside quotes
    (``echo "a # b"``) is still ordinary text rather than a comment.

    Args:
        command: A single shell command line.

    Returns:
        The tokens of ``command`` up to any inline comment, quotes resolved.
    """
    return shlex.split(command.rstrip().rstrip("\\"), comments=True)


def load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML document into a mapping.

    Args:
        path: Path to the YAML file.

    Returns:
        The parsed top-level mapping. Parsing (rather than scanning text)
        is what drops YAML comments, several of which quote the command
        the step actually runs.
    """
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def workflow_files() -> list[Path]:
    """Return every GitHub Actions workflow file in the repository.

    Returns:
        Sorted ``.yml``/``.yaml`` paths under ``.github/workflows``.
    """
    found = [*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")]
    return sorted(found)


def workflow_steps(workflow: Path) -> list[dict[str, Any]]:
    """Return every step of every job in ``workflow``.

    Jobs that delegate to a reusable workflow carry no ``steps`` key, and
    are skipped rather than treated as an error.

    Args:
        workflow: Path to a GitHub Actions workflow file.

    Returns:
        The step mappings, in job-then-step order.
    """
    document = load_yaml(workflow)
    steps: list[dict[str, Any]] = []
    for job in document.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        steps.extend(
            step for step in (job.get("steps") or []) if isinstance(step, dict)
        )
    return steps


_AGENT_ACTION = "anthropics/claude-code-action"


def _effective_issues_permission(
    document: dict[str, Any], job: dict[str, Any]
) -> str | None:
    """Return the ``issues:`` permission in force for one job.

    A job-level ``permissions:`` block replaces the workflow-level one
    outright rather than merging into it, so a job that declares any
    permissions and omits ``issues`` has none.

    Args:
        document: The parsed workflow document.
        job: The parsed job mapping.

    Returns:
        The permission string (``"read"``, ``"write"``, …), or ``None``
        when neither level grants ``issues``.
    """
    permissions = job.get("permissions")
    if not isinstance(permissions, dict):
        permissions = document.get("permissions")
    if not isinstance(permissions, dict):
        return None
    granted = permissions.get("issues")
    return str(granted) if granted is not None else None


def agent_issue_filing_jobs() -> list[tuple[str, str]]:
    """Return every job that runs a Claude agent able to FILE issues.

    Derived, never enumerated by hand (issue #1700). The population is
    "runs ``anthropics/claude-code-action``" crossed with "the job's
    effective ``issues:`` permission is ``write``" -- so a workflow drops
    out because its token cannot create an issue, not because somebody
    remembered to exclude it. Flipping such a workflow to ``issues:
    write`` puts it in the population immediately, which is what makes
    the disposition table in ``test_scan_citations.py`` an enforced
    document rather than a decaying prose one.

    Returns:
        Sorted ``(workflow file name, job id)`` pairs.
    """
    found: list[tuple[str, str]] = []
    for path in workflow_files():
        document = load_yaml(path)
        for job_name, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
            if not any(str(s.get("uses", "")).startswith(_AGENT_ACTION) for s in steps):
                continue
            if _effective_issues_permission(document, job) == "write":
                found.append((path.name, str(job_name)))
    return sorted(found)


def all_workflow_steps() -> list[dict[str, Any]]:
    """Return every step of every job of every workflow in the repository.

    Returns:
        The step mappings across all workflow files.
    """
    steps: list[dict[str, Any]] = []
    for workflow in workflow_files():
        steps.extend(workflow_steps(workflow))
    return steps


def ci_steps() -> list[dict[str, Any]]:
    """Return every step of every job in the root CI workflow.

    Returns:
        The step mappings of ``.github/workflows/ci.yml``.
    """
    return workflow_steps(CI_WORKFLOW)


def step_run_lines(steps: list[dict[str, Any]], pattern: str) -> list[str]:
    """Return the ``run:`` lines of ``steps`` matching ``pattern``.

    Args:
        steps: Workflow step mappings, e.g. from :func:`ci_steps`.
        pattern: Regular expression searched against each stripped line of
            each step's ``run`` block.

    Returns:
        The matching command lines, stripped of surrounding whitespace.
    """
    regex = re.compile(pattern)
    lines: list[str] = []
    for step in steps:
        run_block = step.get("run")
        if not isinstance(run_block, str):
            continue
        for raw_line in run_block.splitlines():
            line = raw_line.strip()
            if regex.search(line):
                lines.append(line)
    return lines
