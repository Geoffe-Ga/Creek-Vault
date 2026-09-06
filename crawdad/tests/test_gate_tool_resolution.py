"""A gate must check *this* project, not whatever copy ``PATH`` finds.

Issue #1671. Every script in ``scripts/`` invoked its tool by bare name,
so bash resolved it through ``PATH``. When the tool was missing from the
environment the gate exists to check, ``PATH`` did not fail -- it kept
walking and found a copy elsewhere. The same one behaviour produced both
reported symptoms:

* ``pip-audit`` was absent from ``crawdad/.venv``, so the security gate
  ran Homebrew's copy and audited ``/opt/homebrew/opt/python@3.13``. It
  reported two CVEs (``click`` 8.3.1, ``pip`` 26.1.2) belonging to that
  interpreter and exited non-zero. Read quickly, the gate looked like it
  was working.
* ``vulture`` was absent too, and the shared dead-code policy imports it
  rather than shelling out, so that step died at ``from vulture import
  Vulture`` with a traceback and no pointer to the fix.

The false *red* trains everyone to dismiss the gate's output. The false
*green* underneath it is worse: while the audit was inspecting Homebrew's
interpreter it was, by definition, not inspecting this project's, so an
advisory present only in the installed environment would go unreported by
the gate that exists to catch it.

**Why this module exists separately from ``test_dependency_pins.py``.**
That module already asserts *which surfaces* ``security.sh`` audits --
the installed environment and the exported lock. It says nothing about
how the tool is resolved, and it cannot: reverting the invocation to a
bare, ``PATH``-resolved ``pip-audit`` leaves every assertion there green,
which was verified by mutation before this file was written. Surface
coverage and interpreter resolution are two independent properties, and
losing either reintroduces #1671.

**These tests execute the guard, they do not only read it.** The static
assertions below pin the shape of the invocations; the behavioural ones
source ``_lib.sh`` in a real bash subprocess and check that an absent
module produces a non-zero exit and a message naming the fix. A guard
asserted only statically is a guard nobody has ever seen run.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_LIB = _SCRIPTS / "_lib.sh"
_SECURITY_SCRIPT = _SCRIPTS / "security.sh"
_VULTURE_SCRIPT = _SCRIPTS / "lint-vulture.sh"

#: The helper every gate step calls before running its tool.
_PROBE = "crawdad_require_python_module"

#: The command its failure message must name. An error the reader cannot
#: act on is only marginally better than the traceback it replaced.
_REMEDIATION = "uv sync --all-extras"

#: Modules whose invocation form this module pins, and the script that
#: must run each through the active interpreter.
_GATED_MODULES = (("pip_audit", _SECURITY_SCRIPT),)


def _commands(script: Path) -> list[str]:
    """Return the executable command lines of ``script``.

    Comment-only lines are dropped and backslash continuations joined, so
    a multi-line invocation is inspected as the one command bash runs and
    prose quoting a command cannot satisfy an assertion about it.

    Args:
        script: Path to the shell script to read.

    Returns:
        One entry per non-blank, non-comment logical line.
    """
    joined = script.read_text(encoding="utf-8").replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _tokenized(script: Path) -> list[list[str]]:
    """Return ``script``'s command lines, shell-split.

    Args:
        script: Path to the shell script to read.

    Returns:
        Each executable line as its list of shell tokens. Lines that do
        not tokenize (an unbalanced quote inside a heredoc, say) are
        skipped rather than failing the run.
    """
    tokenized: list[list[str]] = []
    for command in _commands(script):
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        if tokens:
            tokenized.append(tokens)
    return tokenized


def _is_probe(tokens: list[str]) -> bool:
    """Return whether a command is the guard helper rather than a tool run.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when the line calls :data:`_PROBE`.
    """
    return bool(tokens) and tokens[0] == _PROBE


def _runs_module(tokens: list[str], module: str) -> bool:
    """Return whether a command executes ``module`` via ``python -m``.

    Args:
        tokens: Shell tokens of a single command.
        module: The importable module name, e.g. ``pip_audit``.

    Returns:
        ``True`` for ``python -m <module> ...`` under any ``python``
        spelling, including ``python3`` and an absolute path.
    """
    if len(tokens) < 3:
        return False
    interpreter = tokens[0].rsplit("/", maxsplit=1)[-1]
    return (
        interpreter.startswith("python") and tokens[1] == "-m" and tokens[2] == module
    )


def _mentions_tool_by_bare_name(tokens: list[str], executable: str) -> bool:
    """Return whether a command runs ``executable`` as a PATH-resolved name.

    Args:
        tokens: Shell tokens of a single command.
        executable: The console-script name, e.g. ``pip-audit``.

    Returns:
        ``True`` when the command's own argv[0] is that executable --
        the shape #1671 replaced. A mention in a later argument (the
        probe's display-name argument) is not a run.
    """
    if not tokens or _is_probe(tokens):
        return False
    return tokens[0].rsplit("/", maxsplit=1)[-1] == executable


def _probe_arguments(script: Path) -> list[list[str]]:
    """Return the arguments of each guard call in ``script``.

    Args:
        script: Path to the shell script to read.

    Returns:
        The tokens following :data:`_PROBE` for each call, in order.
    """
    return [tokens[1:] for tokens in _tokenized(script) if _is_probe(tokens)]


def _run_probe(module: str) -> subprocess.CompletedProcess[str]:
    """Source ``_lib.sh`` in bash and invoke the guard for ``module``.

    Args:
        module: The module name to probe for.

    Returns:
        The completed bash process, with text streams captured.
    """
    script = f'set -euo pipefail; source "{_LIB}"; {_PROBE} {shlex.quote(module)}'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_SCRIPTS.parent),
    )


def test_the_scripts_this_module_pins_all_exist() -> None:
    """A missing script must fail loudly, not empty every assertion below.

    Rename or move one of these and each "no bad invocation is present"
    test would pass against an empty corpus -- the standing false-green
    shape on this repository.
    """
    for script in (_LIB, _SECURITY_SCRIPT, _VULTURE_SCRIPT):
        assert script.is_file(), f"{script} does not exist"
        assert _commands(script), f"{script} has no executable command lines"


def test_every_pip_audit_run_goes_through_the_active_interpreter() -> None:
    """No gate audit may be a bare, ``PATH``-resolved executable.

    This is the assertion #1671 turns on, and the one
    ``test_dependency_pins.py`` structurally cannot make: that module
    asks *which surfaces* are audited, and both of its answers stay true
    when the invocation reverts to a bare ``pip-audit`` that audits the
    wrong interpreter.
    """
    for module, script in _GATED_MODULES:
        executable = module.replace("_", "-")
        bare = [
            tokens
            for tokens in _tokenized(script)
            if _mentions_tool_by_bare_name(tokens, executable)
        ]
        assert not bare, (
            f"{script.name} runs `{executable}` as a bare name, so PATH "
            f"decides which copy runs and therefore which environment is "
            f"checked. Use `python -m {module}`. Found: {bare!r}"
        )
        module_runs = [
            tokens for tokens in _tokenized(script) if _runs_module(tokens, module)
        ]
        assert module_runs, (
            f"{script.name} no longer runs `python -m {module}` at all; "
            f"the {executable} gate has been removed rather than fixed"
        )


def test_each_gate_probes_for_its_tool_before_running_it() -> None:
    """Both repaired steps must guard, and guard the module they use.

    ``security.sh`` probes ``pip_audit``; ``lint-vulture.sh`` probes
    ``vulture``, which the shared policy imports rather than executing.
    """
    expected = {_SECURITY_SCRIPT: "pip_audit", _VULTURE_SCRIPT: "vulture"}
    for script, module in expected.items():
        probes = _probe_arguments(script)
        assert probes, (
            f"{script.name} calls no {_PROBE}, so a missing tool is "
            f"resolved from PATH (or dies at import) instead of being "
            f"reported with the command that fixes it"
        )
        probed = {arguments[0] for arguments in probes if arguments}
        assert module in probed, (
            f"{script.name} probes {sorted(probed)} but runs `{module}`; "
            "the guard and the tool must name the same module or the "
            "guard passes while the tool is absent"
        )


def test_the_probe_runs_before_the_tool_it_guards() -> None:
    """A guard placed after the tool it guards never fires.

    ``set -euo pipefail`` means the failing audit aborts the script
    first, so ordering is the whole of the guard's value.
    """
    tokenized = _tokenized(_SECURITY_SCRIPT)
    probe_at = next(
        (index for index, tokens in enumerate(tokenized) if _is_probe(tokens)), None
    )
    audit_at = next(
        (
            index
            for index, tokens in enumerate(tokenized)
            if _runs_module(tokens, "pip_audit")
        ),
        None,
    )
    assert probe_at is not None, "security.sh has no probe"
    assert audit_at is not None, "security.sh runs no pip_audit audit"
    assert probe_at < audit_at, (
        f"security.sh probes at command {probe_at} but audits at "
        f"{audit_at}; a guard after the step it guards cannot fire"
    )


def test_the_vulture_gate_probes_before_it_execs_the_policy() -> None:
    """``exec`` replaces the shell, so anything after it never runs."""
    tokenized = _tokenized(_VULTURE_SCRIPT)
    probe_at = next(
        (index for index, tokens in enumerate(tokenized) if _is_probe(tokens)), None
    )
    exec_at = next(
        (index for index, tokens in enumerate(tokenized) if tokens[0] == "exec"), None
    )
    assert probe_at is not None, "lint-vulture.sh has no probe"
    assert exec_at is not None, "lint-vulture.sh no longer execs the policy module"
    assert probe_at < exec_at, (
        "lint-vulture.sh probes after its `exec`; exec replaces the "
        "process image, so the guard would never run"
    )


def test_a_missing_module_fails_the_gate_with_the_fix_named() -> None:
    """The guard must fail, not skip, and must say what to run.

    A dead-code or audit step that skipped would be indistinguishable
    from one that ran and found nothing -- the failure mode #1186/#1187
    were about. So this asserts a non-zero exit *and* that the message
    carries the remediation.
    """
    result = _run_probe("crawdad_no_such_module_1671")
    assert result.returncode != 0, (
        "the guard exited 0 for a module that cannot be imported, so a "
        f"gate would proceed against the wrong environment: {result!r}"
    )
    assert _REMEDIATION in result.stderr, (
        f"the guard's message does not name `{_REMEDIATION}`, leaving the "
        f"reader with no action to take. stderr was: {result.stderr!r}"
    )


def test_the_guard_is_a_no_op_when_the_module_is_present() -> None:
    """A correctly provisioned environment must stay silent and green.

    Probing for a stdlib module proves the success path without
    depending on any dev extra being installed.
    """
    result = _run_probe("json")
    assert result.returncode == 0, (
        f"the guard failed for a stdlib module, so it would redden every "
        f"correctly provisioned run: {result.stderr!r}"
    )
    assert not result.stderr.strip(), (
        f"the guard wrote to stderr on the success path: {result.stderr!r}"
    )


def test_the_probe_is_not_mistaken_for_an_audit() -> None:
    """The guard line names ``pip-audit``; it must not count as one.

    ``security.sh``'s probe passes ``pip-audit`` as a display name, so a
    recognizer keying on the string alone would treat the guard itself as
    the environment audit -- and stay green after the real audit was
    deleted. That is not hypothetical: it happened while writing this
    change, and ``_PROBE_COMMANDS`` in ``test_dependency_pins.py`` is the
    other half of the fix.
    """
    probe_line = f"{_PROBE} pip_audit pip-audit || exit 2"
    tokens = shlex.split(probe_line)
    assert "pip-audit" in tokens, "the fixture no longer reproduces the hazard"
    assert not _mentions_tool_by_bare_name(tokens, "pip-audit")
    assert not _runs_module(tokens, "pip_audit")


def test_commented_prose_cannot_satisfy_these_assertions() -> None:
    """Both scripts explain the fix in comments that quote the command.

    Comment-blindness is what separates this module from a substring
    scan that would pass against the prose while the executing line said
    something else.
    """
    commented = "# python -m pip_audit"
    assert commented not in _commands(_SECURITY_SCRIPT)
    genuine = [
        command
        for command in _commands(_SECURITY_SCRIPT)
        if re.fullmatch(r"python -m pip_audit", command)
    ]
    assert genuine, "the bare environment audit line is no longer present"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m pip_audit", True),
        ("python3 -m pip_audit", True),
        ("/usr/bin/python3.13 -m pip_audit", True),
        ("python -m pip_audit --requirement reqs.txt", True),
        ("pip-audit", False),
        ("python -m pip", False),
        ("python -c import pip_audit", False),
        ("python", False),
    ],
)
def test_the_interpreter_recognizer_reads_each_spelling(
    command: str, expected: bool
) -> None:
    """The recognizer must not answer ``True`` for near-misses.

    A predicate that over-matches would let ``python -m pip`` satisfy the
    audit assertion; one that under-matches would redden a legitimate
    ``python3`` spelling on a machine where that is the interpreter.

    Args:
        command: The shell command under test.
        expected: Whether it runs ``pip_audit`` through the interpreter.
    """
    assert _runs_module(shlex.split(command), "pip_audit") is expected


def test_the_probe_uses_the_same_interpreter_the_gate_runs() -> None:
    """The guard must probe ``python``, not a hard-coded interpreter path.

    Probing one interpreter and running another is the original defect
    wearing a different hat: the answer would describe an environment
    that is not the one under test.
    """
    body = _LIB.read_text(encoding="utf-8")
    assert 'python -c "import ${module}"' in body, (
        "the guard no longer probes with a bare `python`, so it may be "
        "asking a different interpreter than the one the gate then uses"
    )
    assert sys.executable, "no interpreter reported; the environment is unusable"
