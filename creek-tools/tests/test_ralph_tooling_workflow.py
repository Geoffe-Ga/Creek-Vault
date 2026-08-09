"""Every suite under ``scripts/ralph/`` must actually run in CI.

``.github/workflows/ralph-recap-tests.yml`` is the only workflow that runs
the Ralph tooling suites, and it lists each shell suite by name. Its own
header states why: *"a suite that is written but never wired in here is not
a gate, it is a file."* That is the right rule and the wrong enforcement —
a rule kept by hand in a `run:` block is kept only for as long as everyone
remembers it, and #1141 forgot it the first time it mattered, shipping a
206-line ``test_pr_status.sh`` that no workflow ever invoked.

So the list is checked against the filesystem rather than against a second
list. Retyping the seven suite names here would move the same defect one
level up: a new suite would be missing from both places and both would
agree. Instead every ``scripts/ralph/test_*.sh`` on disk must appear in the
workflow's ``run:`` block, and every ``scripts/ralph/test_*.py`` must fall
under one of its pytest targets. Adding a suite and not wiring it in is
then a failing test, not a code-review catch.

The second half is the same invariant pointed the other way. A suite that
runs only when its *own* directory changes is blind to a change in the
script it tests. Every other suite tests a sibling under ``scripts/ralph/``,
which the ``scripts/ralph/**`` trigger covers by accident; ``pr-status.sh``
lives under ``creek-tools/scripts/`` and does not. The subject of a suite is
derived from its name (``test_<x>.sh`` guards ``<x>.sh``) so the next
out-of-tree subject cannot be forgotten either.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from tests.shell_command_support import (
    CREEK_TOOLS_DIR,
    RALPH_SCRIPTS_DIR,
    RALPH_WORKFLOW,
    REPO_ROOT,
    load_yaml,
    shell_tokens,
    step_run_lines,
    workflow_steps,
)

# `bash scripts/ralph/test_x.sh` — how the workflow invokes a shell suite.
_SHELL_SUITE_RE = re.compile(r"\bbash\s+(scripts/ralph/test_[\w.-]+\.sh)")

# Directories searched for the script a suite is named after, in order.
_SUBJECT_DIRS = (RALPH_SCRIPTS_DIR, CREEK_TOOLS_DIR / "scripts")

_TRIGGER_EVENTS = ("push", "pull_request")


def _workflow() -> dict[str, object]:
    """Return the parsed Ralph tooling workflow document."""
    return load_yaml(RALPH_WORKFLOW)


def _run_lines() -> list[str]:
    """Return every non-blank ``run:`` line in the workflow.

    Returns:
        The command lines, comments dropped by the YAML parse.
    """
    return step_run_lines(workflow_steps(RALPH_WORKFLOW), r"\S")


def _wired_shell_suites() -> set[str]:
    """Return the shell suites the workflow invokes, as repo-relative paths."""
    return {
        match.group(1)
        for line in _run_lines()
        for match in [_SHELL_SUITE_RE.search(line)]
        if match is not None
    }


def _relative(path: Path) -> str:
    """Return ``path`` as a repo-relative POSIX string.

    Args:
        path: An absolute path inside the repository.

    Returns:
        The path relative to the repository root, ``/``-separated.
    """
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _shell_suites_on_disk() -> set[str]:
    """Return every ``scripts/ralph/test_*.sh`` present in the working tree."""
    return {_relative(path) for path in RALPH_SCRIPTS_DIR.glob("test_*.sh")}


def _python_suites_on_disk() -> set[str]:
    """Return every ``scripts/ralph/test_*.py`` present in the working tree."""
    return {_relative(path) for path in RALPH_SCRIPTS_DIR.glob("test_*.py")}


def _pytest_targets() -> list[str]:
    """Return the paths the workflow hands to pytest.

    A target may be a directory (``scripts/ralph``) or a single file; both
    spellings are returned verbatim, repo-relative.

    Returns:
        The non-flag arguments of every ``pytest`` invocation.
    """
    targets: list[str] = []
    for line in _run_lines():
        tokens = shell_tokens(line)
        if "pytest" not in tokens:
            continue
        index = tokens.index("pytest")
        targets.extend(
            token
            for token in tokens[index + 1 :]
            if not token.startswith("-") and (REPO_ROOT / token).exists()
        )
    return targets


def _is_collected(suite: str, targets: list[str]) -> bool:
    """Report whether ``suite`` lies under one of ``targets``.

    Args:
        suite: Repo-relative path of a Python suite.
        targets: Repo-relative pytest targets, files or directories.

    Returns:
        ``True`` when pytest would collect the suite.
    """
    return any(
        suite == target or suite.startswith(f"{target.rstrip('/')}/")
        for target in targets
    )


def _subject_script(suite: str) -> str | None:
    """Return the script a suite is named after, if it exists.

    The convention is ``test_<slug>.sh`` guards ``<slug>.sh``, with
    underscores in the slug standing in for the subject's hyphens
    (``test_pr_status.sh`` → ``pr-status.sh``). Suites with no matching
    script — ``test_exec_bits.sh`` guards a file *mode*, not a file — yield
    ``None`` rather than an error.

    Args:
        suite: Repo-relative path of a shell suite.

    Returns:
        The subject's repo-relative path, or ``None`` when there is none.
    """
    slug = Path(suite).stem.removeprefix("test_")
    names = {f"{slug}.sh", f"{slug.replace('_', '-')}.sh"}
    for directory in _SUBJECT_DIRS:
        for name in sorted(names):
            candidate = directory / name
            if candidate.is_file():
                return _relative(candidate)
    return None


def _trigger_paths(event: str) -> list[str]:
    """Return the ``paths:`` filter of one trigger event.

    Args:
        event: ``"push"`` or ``"pull_request"``.

    Returns:
        The configured path patterns.
    """
    # PyYAML resolves the bare `on:` key to the boolean True (YAML 1.1), so
    # the string spelling is tried first and the boolean is the fallback.
    document = _workflow()
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), f"{RALPH_WORKFLOW.name} has no `on:` block"
    config = triggers.get(event)
    assert isinstance(config, dict), (
        f"{RALPH_WORKFLOW.name} lost its `{event}:` trigger"
    )
    paths = config.get("paths")
    assert isinstance(paths, list), f"`{event}:` no longer filters on `paths:`"
    return [str(pattern) for pattern in paths]


def _is_triggered_by(path: str, patterns: list[str]) -> bool:
    """Report whether a change to ``path`` matches any trigger pattern.

    Args:
        path: Repo-relative path of a file.
        patterns: GitHub Actions path filters.

    Returns:
        ``True`` when at least one pattern selects the file.
    """
    return any(
        pattern == path
        or (pattern.endswith("/**") and path.startswith(pattern[: -len("**")]))
        or fnmatch(path, pattern)
        for pattern in patterns
    )


def test_every_ralph_shell_suite_is_wired_into_the_workflow() -> None:
    """No ``test_*.sh`` may exist under ``scripts/ralph/`` without running.

    This is the rule the workflow's own comment states, enforced against
    the filesystem so that writing a suite and forgetting to list it is a
    red build rather than an unrun file (#1141).
    """
    unwired = sorted(_shell_suites_on_disk() - _wired_shell_suites())
    assert not unwired, (
        f"{unwired} exist under scripts/ralph/ but are never invoked by "
        f"{RALPH_WORKFLOW.name}; a suite that no workflow runs is not a gate, "
        "it is a file — add `bash <suite>` to that job's run block"
    )


def test_every_wired_shell_suite_exists_on_disk() -> None:
    """The workflow must not invoke a suite that was renamed or deleted.

    ``bash`` on a missing path fails the job loudly, so this catches the
    rename at review time instead of on the next unrelated push.
    """
    missing = sorted(_wired_shell_suites() - _shell_suites_on_disk())
    assert not missing, (
        f"{RALPH_WORKFLOW.name} invokes {missing}, which do not exist; the "
        "job would fail on every run for a reason unrelated to the change"
    )


def test_every_ralph_python_suite_is_collected() -> None:
    """Each ``scripts/ralph/test_*.py`` must fall under a pytest target.

    The Python half is wired by directory rather than by name, so the check
    is containment: a suite outside every target is as unrun as an unlisted
    shell suite.
    """
    targets = _pytest_targets()
    assert targets, f"{RALPH_WORKFLOW.name} no longer runs pytest on anything"
    uncollected = sorted(
        suite for suite in _python_suites_on_disk() if not _is_collected(suite, targets)
    )
    assert not uncollected, (
        f"{uncollected} are not under any pytest target of "
        f"{RALPH_WORKFLOW.name} (targets: {targets})"
    )


def test_each_suite_runs_when_its_subject_script_changes() -> None:
    """A suite must be triggered by edits to the script it guards.

    Sibling subjects are covered by the ``scripts/ralph/**`` filter for
    free. ``pr-status.sh`` is not a sibling — it lives under
    ``creek-tools/scripts/`` — so before #1141 a change to the script under
    test ran no suite at all. Deriving the subject from the suite name
    keeps the next out-of-tree subject from repeating that.
    """
    for event in _TRIGGER_EVENTS:
        patterns = _trigger_paths(event)
        blind = sorted(
            subject
            for suite in _wired_shell_suites()
            for subject in [_subject_script(suite)]
            if subject is not None and not _is_triggered_by(subject, patterns)
        )
        assert not blind, (
            f"{RALPH_WORKFLOW.name}'s `{event}:` paths do not select {blind}, "
            "so a change to a script under test would run no suite; add each "
            "path to the filter"
        )


def test_both_trigger_events_filter_on_the_same_paths() -> None:
    """``push`` and ``pull_request`` must watch an identical path set.

    The two lists exist because a ``push``-only filter lets drift land
    through a PR and a ``pull_request``-only filter misses a direct push to
    ``main``. They only provide that if they stay in step, and they are
    maintained by copy-paste.
    """
    push, pull_request = (_trigger_paths(event) for event in _TRIGGER_EVENTS)
    assert sorted(push) == sorted(pull_request), (
        f"{RALPH_WORKFLOW.name}'s push/pull_request path filters diverged: "
        f"push-only={sorted(set(push) - set(pull_request))}, "
        f"pr-only={sorted(set(pull_request) - set(push))}"
    )
