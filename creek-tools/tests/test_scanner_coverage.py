"""Static-analysis scanners must cover ``creek_mcp/``, not only ``creek/``.

Issue #925: ``scripts/typecheck.sh``, ``scripts/security.sh``,
``scripts/pylint.sh``, ``scripts/lint-extended.sh``, the CI "Run Bandit
security scan" step and the ``pylint-fast`` pre-commit hook each aim their
tool at ``creek/`` alone. The entire ``creek_mcp/`` package -- the MCP
server plus its auth, token-policy and path-confinement code -- is therefore
never type-checked, security-scanned or lint-gated. These tests are the gate
contract that keeps every one of those invocations widened.

They also carry anti-weakening guards: the widened scan must not be made to
pass by trading away Bandit's ``-ll`` severity threshold, Pylint's
``--fail-under`` score threshold, by re-excluding ``creek_mcp`` from
``pyproject.toml``'s Bandit/MyPy configuration, or by naming ``creek_mcp``
as a target and then carving it back out with an ``--exclude`` /
``--ignore-paths`` flag on the same command line.

Command lines are read with shell and YAML comments stripped first. Several
of these files quote the very command they invoke inside an explanatory
comment (``typecheck.sh`` line 57, ``pylint.sh`` line 66, ``ci.yml`` line
117), so a naive substring scan would assert against prose rather than
against the command that actually runs.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

import yaml

CREEK_TOOLS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CREEK_TOOLS_DIR.parent
SCRIPTS_DIR = CREEK_TOOLS_DIR / "scripts"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT_CONFIG = CREEK_TOOLS_DIR / ".pre-commit-config.yaml"
PYPROJECT = CREEK_TOOLS_DIR / "pyproject.toml"

# ``\bcreek\b`` deliberately does NOT match inside ``creek_mcp`` (``_`` is a
# word character), so the two probes are independent and either spelling of
# the widened target (``creek/ creek_mcp/`` or ``creek creek_mcp``) passes.
_CREEK_TARGET = re.compile(r"\bcreek\b")
_CREEK_MCP_TARGET = re.compile(r"\bcreek_mcp\b")

# Captures the numeric default out of ``FAIL_UNDER="${PYLINT_FAIL_UNDER:-9.0}"``
# without pinning the exact string, so a reformat is fine but a lowered
# threshold is not.
_FAIL_UNDER_DEFAULT = re.compile(
    r'FAIL_UNDER="\$\{PYLINT_FAIL_UNDER:-([0-9]+(?:\.[0-9]+)?)\}"'
)

# Flags that carve a path back out of an already-widened target list, as
# spelled by mypy (``--exclude``), pylint (``--ignore``/``--ignore-paths``/
# ``--ignore-patterns``) and bandit (``-x``/``--exclude``). Naming
# ``creek_mcp`` on the command line is necessary but not sufficient: an
# ``--exclude 'creek_mcp/remote_auth\.py'`` would satisfy every
# word-presence probe above while switching the scan back off.
_EXCLUSION_FLAGS = frozenset(
    {"--exclude", "-x", "--ignore", "--ignore-paths", "--ignore-patterns"}
)

# MyPy settings that would silently switch checking back off for a module.
# Mapping is ``setting -> value that disables checking``.
_MYPY_DISABLING_SETTINGS: dict[str, Any] = {
    "ignore_errors": True,
    "follow_imports": "skip",
    "disallow_untyped_defs": False,
    "check_untyped_defs": False,
}


def _non_comment_lines(script: Path) -> list[str]:
    """Return the lines of ``script`` that are not shell comments.

    A line whose left-stripped form starts with ``#`` is a comment and is
    dropped, so prose that quotes a command cannot satisfy an assertion
    about the command that actually executes.
    """
    return [
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def _command_lines(script: Path, pattern: str) -> list[str]:
    """Return non-comment lines of ``script`` matching ``pattern``."""
    regex = re.compile(pattern)
    return [line.strip() for line in _non_comment_lines(script) if regex.search(line)]


def _exclusion_values(command: str) -> list[str]:
    """Return every value passed to a path-exclusion flag in ``command``.

    Handles both ``--exclude value`` and ``--exclude=value`` spellings. A
    trailing shell line-continuation backslash is stripped first so
    :func:`shlex.split` does not choke on a dangling escape.
    """
    values: list[str] = []
    tokens = shlex.split(command.rstrip().rstrip("\\"))
    for index, token in enumerate(tokens):
        flag, separator, inline = token.partition("=")
        if flag not in _EXCLUSION_FLAGS:
            continue
        if separator:
            values.append(inline)
        elif index + 1 < len(tokens):
            values.append(tokens[index + 1])
    return values


def _all_scanner_invocations() -> list[str]:
    """Return every gating mypy/bandit/pylint command line under test."""
    return [
        *_command_lines(SCRIPTS_DIR / "typecheck.sh", r"python -m mypy"),
        *_command_lines(SCRIPTS_DIR / "security.sh", r"bandit -r"),
        *_command_lines(SCRIPTS_DIR / "pylint.sh", r"python -m pylint"),
        *_command_lines(SCRIPTS_DIR / "lint-extended.sh", r"^\s*pylint\s"),
        *_ci_bandit_run_lines(),
    ]


def _assert_targets_both_packages(lines: list[str], source: str) -> None:
    """Assert every command line in ``lines`` names ``creek`` and ``creek_mcp``."""
    for line in lines:
        assert _CREEK_TARGET.search(line), (
            f"{source}: invocation lost its `creek/` target: {line!r}"
        )
        assert _CREEK_MCP_TARGET.search(line), (
            f"{source}: invocation does not scan `creek_mcp/` (issue #925): {line!r}"
        )


def _as_list(value: Any) -> list[str]:
    """Normalise a TOML scalar-or-list field to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _ci_steps() -> list[dict[str, Any]]:
    """Return every step of every job in the root CI workflow."""
    workflow: dict[str, Any] = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _ci_bandit_run_lines() -> list[str]:
    """Return the ``bandit`` command lines of the CI Bandit step.

    Parsing via ``yaml.safe_load`` drops the step's explanatory comment,
    which itself quotes ``bandit -r creek/ -ll``.
    """
    bandit_steps = [step for step in _ci_steps() if "Bandit" in str(step.get("name"))]
    assert len(bandit_steps) == 1, (
        f"expected exactly one CI step named for Bandit, found {len(bandit_steps)}"
    )
    run_block = str(bandit_steps[0]["run"])
    return [
        line.strip()
        for line in run_block.splitlines()
        if line.strip().startswith("bandit")
    ]


def _pylint_fast_hook() -> dict[str, Any] | None:
    """Return the local ``pylint-fast`` pre-commit hook, or ``None``."""
    config: dict[str, Any] = yaml.safe_load(
        PRE_COMMIT_CONFIG.read_text(encoding="utf-8")
    )
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if hook.get("id") == "pylint-fast":
                return dict(hook)
    return None


def _pylint_fast_files_regex() -> re.Pattern[str]:
    """Return the compiled ``files`` regex of the ``pylint-fast`` hook."""
    hook = _pylint_fast_hook()
    assert hook is not None, "no `pylint-fast` hook in .pre-commit-config.yaml"
    files = hook.get("files")
    assert files, "`pylint-fast` hook has no `files` filter to assert on"
    return re.compile(str(files))


def _pyproject() -> dict[str, Any]:
    """Return the parsed ``creek-tools/pyproject.toml``."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_scanner_config_files_all_exist() -> None:
    """Guard the path constants so no other test can pass vacuously."""
    for path in (
        SCRIPTS_DIR / "typecheck.sh",
        SCRIPTS_DIR / "security.sh",
        SCRIPTS_DIR / "pylint.sh",
        SCRIPTS_DIR / "lint-extended.sh",
        CI_WORKFLOW,
        PRE_COMMIT_CONFIG,
        PYPROJECT,
    ):
        assert path.is_file(), f"expected scanner config at {path}"
    assert (CREEK_TOOLS_DIR / "creek_mcp").is_dir(), (
        "creek_mcp/ package is missing; the widening these tests pin is moot"
    )


def test_typecheck_script_runs_mypy_over_creek_mcp() -> None:
    """scripts/typecheck.sh must type-check creek_mcp/ as well as creek/."""
    script = SCRIPTS_DIR / "typecheck.sh"
    lines = _command_lines(script, r"python -m mypy")
    assert lines, "no `python -m mypy` invocation found in scripts/typecheck.sh"
    _assert_targets_both_packages(lines, "scripts/typecheck.sh")


def test_security_script_scans_creek_mcp_with_bandit() -> None:
    """scripts/security.sh must run Bandit over creek_mcp/ as well as creek/."""
    script = SCRIPTS_DIR / "security.sh"
    lines = _command_lines(script, r"bandit -r")
    assert lines, "no `bandit -r` invocation found in scripts/security.sh"
    _assert_targets_both_packages(lines, "scripts/security.sh")


def test_ci_bandit_step_scans_creek_mcp() -> None:
    """CI runs Bandit inline, not via security.sh, so it must widen too."""
    lines = _ci_bandit_run_lines()
    assert len(lines) >= 2, (
        f"expected the JSON-artifact and gating bandit invocations, got {lines!r}"
    )
    _assert_targets_both_packages(lines, "ci.yml Run Bandit security scan")


def test_pylint_script_lints_creek_mcp() -> None:
    """Both pylint invocations in scripts/pylint.sh must include creek_mcp/."""
    script = SCRIPTS_DIR / "pylint.sh"
    lines = _command_lines(script, r"python -m pylint")
    assert len(lines) >= 2, (
        f"expected the gating and JSON-snapshot pylint runs, got {lines!r}"
    )
    _assert_targets_both_packages(lines, "scripts/pylint.sh")


def test_lint_extended_script_lints_creek_mcp() -> None:
    """scripts/lint-extended.sh's pylint target line must include creek_mcp/."""
    script = SCRIPTS_DIR / "lint-extended.sh"
    lines = _command_lines(script, r"^\s*pylint\s")
    assert lines, "no `pylint <target>` line found in scripts/lint-extended.sh"
    _assert_targets_both_packages(lines, "scripts/lint-extended.sh")


def test_pre_commit_pylint_hook_matches_creek_mcp_files() -> None:
    """The pylint-fast hook's `files` regex must select creek_mcp/ sources."""
    pattern = _pylint_fast_files_regex()
    assert pattern.search("creek-tools/creek_mcp/server.py"), (
        f"pylint-fast `files` regex {pattern.pattern!r} skips creek_mcp/ (issue #925)"
    )


def test_pre_commit_pylint_hook_still_matches_creek_and_skips_tests() -> None:
    """Widening the pylint-fast hook must not lose creek/ or pull in tests/."""
    pattern = _pylint_fast_files_regex()
    assert pattern.search("creek-tools/creek/cli.py"), (
        f"pylint-fast `files` regex {pattern.pattern!r} stopped matching creek/"
    )
    assert not pattern.search("creek-tools/tests/test_x.py"), (
        f"pylint-fast `files` regex {pattern.pattern!r} now sweeps in tests/"
    )


def test_bandit_gate_keeps_medium_severity_threshold() -> None:
    """Widening Bandit must not drop the `-ll` medium-or-above gate."""
    script_gate = [
        line
        for line in _command_lines(SCRIPTS_DIR / "security.sh", r"bandit -r")
        if "|| true" not in line
    ]
    assert len(script_gate) == 1, (
        f"expected exactly one gating bandit line in security.sh, got {script_gate!r}"
    )
    assert "-ll" in script_gate[0], (
        f"security.sh bandit gate lost its `-ll` threshold: {script_gate[0]!r}"
    )

    ci_gate = [line for line in _ci_bandit_run_lines() if "|| true" not in line]
    assert len(ci_gate) == 1, (
        f"expected exactly one gating bandit line in ci.yml, got {ci_gate!r}"
    )
    assert "-ll" in ci_gate[0], (
        f"ci.yml bandit gate lost its `-ll` threshold: {ci_gate[0]!r}"
    )


def test_pylint_gate_keeps_fail_under_threshold() -> None:
    """Widening Pylint must not drop or lower the --fail-under score gate."""
    script = SCRIPTS_DIR / "pylint.sh"
    gating = [
        line
        for line in _command_lines(script, r"python -m pylint")
        if "--output-format=json" not in line
    ]
    assert len(gating) == 1, (
        f"expected exactly one gating pylint line in pylint.sh, got {gating!r}"
    )
    assert "--fail-under" in gating[0], (
        f"pylint.sh gate no longer passes --fail-under: {gating[0]!r}"
    )

    match = _FAIL_UNDER_DEFAULT.search("\n".join(_non_comment_lines(script)))
    assert match is not None, (
        "could not read the PYLINT_FAIL_UNDER default out of scripts/pylint.sh"
    )
    assert float(match.group(1)) >= 9.0, (
        f"pylint --fail-under default dropped below 9.0: {match.group(1)}"
    )


def test_scanner_targets_are_not_carved_back_out_by_exclusions() -> None:
    """Naming creek_mcp must not be undone by an --exclude/--ignore flag.

    Every other assertion here only checks that ``creek_mcp`` appears
    somewhere on the command line, which a re-narrowing edit such as
    ``mypy creek/ creek_mcp/ --exclude 'creek_mcp/remote_auth\\.py'`` would
    still satisfy -- the excluded path contains the word too. This closes
    that loophole for all three scanners at once.
    """
    invocations = _all_scanner_invocations()
    assert len(invocations) >= 6, (
        f"expected every scanner invocation to be collected, got {invocations!r}"
    )
    for command in invocations:
        offenders = [
            value for value in _exclusion_values(command) if "creek_mcp" in value
        ]
        assert not offenders, (
            f"invocation excludes part of creek_mcp again: {offenders!r} in {command!r}"
        )


def test_pyproject_bandit_config_does_not_exclude_creek_mcp() -> None:
    """A widened Bandit CLI target must not be undone by exclude_dirs."""
    bandit_config = _pyproject()["tool"]["bandit"]
    excluded = _as_list(bandit_config.get("exclude_dirs"))
    offenders = [entry for entry in excluded if "creek_mcp" in entry]
    assert not offenders, (
        f"[tool.bandit] exclude_dirs re-excludes creek_mcp: {offenders!r}"
    )


def test_pyproject_mypy_config_does_not_disable_creek_mcp() -> None:
    """A widened MyPy CLI target must not be undone by an exclude/override."""
    mypy_config = _pyproject()["tool"]["mypy"]

    excluded = [
        entry for entry in _as_list(mypy_config.get("exclude")) if "creek_mcp" in entry
    ]
    assert not excluded, f"[tool.mypy] exclude re-excludes creek_mcp: {excluded!r}"

    for override in mypy_config.get("overrides", []):
        modules = _as_list(override.get("module"))
        if not any("creek_mcp" in module for module in modules):
            continue
        for setting, disabling_value in _MYPY_DISABLING_SETTINGS.items():
            assert override.get(setting, None) != disabling_value, (
                f"[[tool.mypy.overrides]] for {modules!r} disables checking "
                f"via {setting}={disabling_value!r}"
            )
