"""Producer scans must stand down when the agent-ready backlog is deep.

Issue #1516. Measured live on 2026-08-14 against ``Geoffe-Ga/Creek-Vault``:
**311 open issues, 158 of them labelled ``agent-ready``** -- the population
the automated producers file into, and the one ``hopper.yml`` already
measured. The operator's directive is to cap it at **~90**; past the cap,
new issues go stale before anyone can work them, so filing more is not
productivity, it is landfill.

Note what this does *not* claim. ``agent-ready`` is not literally the
picker's input set: ``scripts/ralph/pick-next.sh:57`` defaults
``RALPH_REQUIRE_LABELS`` to empty and ``.claude/commands/ralph-tick.md:479``
calls it with no override, so the live loop draws from every open issue
minus its exclude set. The ceiling bounds what the producers *add*, which
is the gap this module exists to close; it is not a bound on the whole
backlog.

The depth check already existed, but only in one place and only on one
path. ``.github/workflows/hopper.yml`` hardcoded ``MAX_QUEUE: "80"`` and
stood down above it -- but hopper is the *off-schedule refill* path. Every
producer scan also carries its own ``schedule: cron`` and calls the
reusable core ``.github/workflows/_claude-scan.yml`` directly, and that
core had **zero** depth gating. So the one path that exists to *add* work
when the queue is starving was throttled, while the eleven that fire on a
timer regardless were not. That asymmetry is the bug: the backlog reached
158 while a "max 80" guard was, technically, in the repository.

The fix pins one implementation in one file --
``scripts/ralph/backlog-gate.sh`` -- called by the reusable core and by
hopper. This module asserts the *shape* of that wiring and the
*behaviour* of that script.

Two traps this module exists to encode:

* ``grep -l "_claude-scan.yml" .github/workflows/scan-*.yml`` returns
  **12 of 12** files, but only **11** route through the core.
  ``scan-groom.yml`` matches on its header *comment* alone; it is a
  deliberate CONSUMER (it closes resolved/stale issues, dedupes, and
  promotes needs-triage to agent-ready -- "net effect is a queue that
  shrinks", ``prompts/scans/groom.md:19``) with its own thin workflow.
  Gating groom would be self-deadlocking: above the ceiling it is the
  only automated path that lowers the count. Every assertion here
  therefore **parses the YAML** and tokenises shell, via
  ``tests.shell_command_support`` -- never greps -- because a workflow
  that quotes a command in a comment must never satisfy an assertion
  about the command that runs.
* A guard that passes because it enumerated nothing is this repository's
  standing failure mode. Every enumeration below asserts its population
  is non-empty *before* asserting a property of it, and the behavioural
  tests make the stub ``gh`` record its argv so a ``PATH`` mistake can
  never green them against an absent -- or real -- ``gh``.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.shell_command_support import (
    REPO_ROOT,
    WORKFLOWS_DIR,
    load_yaml,
    non_comment_lines,
    shell_tokens,
    workflow_files,
    workflow_steps,
)

if TYPE_CHECKING:
    from pathlib import Path

# --- The contract under test -------------------------------------------

# Path as written in a workflow ``run:`` block, i.e. relative to the repo
# root, which is every job's working directory after checkout. It lives
# under ``scripts/ralph/`` rather than beside the workflows because that
# directory is already covered by two gates this script needs and
# ``.github/`` is covered by neither: ``ralph-recap-tests.yml`` runs
# ``shellcheck --severity=warning scripts/ralph/*.sh`` in CI, and
# ``scripts/ralph/test_exec_bits.sh`` fails any ``scripts/ralph/*.sh``
# whose recorded git mode is not 100755.
_GATE_SCRIPT_REL = "scripts/ralph/backlog-gate.sh"
_GATE_SCRIPT = REPO_ROOT / _GATE_SCRIPT_REL

_CORE_WORKFLOW = WORKFLOWS_DIR / "_claude-scan.yml"
_HOPPER_WORKFLOW = WORKFLOWS_DIR / "hopper.yml"
_GROOM_WORKFLOW = WORKFLOWS_DIR / "scan-groom.yml"
_CORE_USES = "./.github/workflows/_claude-scan.yml"

_GATE_STEP_ID = "backlog_gate"
_CLAUDE_ACTION = "anthropics/claude-code-action"
_OVERRIDE_ENV = "BACKLOG_CEILING"
_OVERRIDE_INPUT = "backlog_ceiling"

# The ceiling, stated once in the script and nowhere else. Restated here
# only so the behavioural tests can assert the script honours its own
# constant; test_backlog_ceiling_is_defined_exactly_once is what forbids
# a second implementation-side copy.
_DEFAULT_CEILING = 90
_CEILING_ASSIGNMENT = f"MAX_AGENT_READY_QUEUE={_DEFAULT_CEILING}"
_CEILING_ASSIGNMENT_RE = re.compile(
    rf"^\s*(readonly\s+)?{_CEILING_ASSIGNMENT}\s*(#.*)?$"
)

# Measured agent-ready depth on 2026-08-14. Used as the "over the ceiling"
# fixture, which makes it the detector for the mutation that matters most:
# raise the ceiling above today's real backlog and the stand-down tests
# would go green by vacuity, so they redden here instead.
_MEASURED_DEPTH = 158

# Wrapper census on 2026-08-14: 12 ``scan-*.yml`` files, 11 of which route
# through the gated core. See the module docstring for why groom is out.
_CORE_ROUTED_WRAPPERS = 11
_CONSUMER_ONLY_WRAPPERS = frozenset({"scan-groom.yml"})
_GROOM_RATIONALE = (
    "scan-groom.yml is the one justified BYPASS: it is a CONSUMER that "
    "shrinks the queue (closes resolved/stale issues, dedupes, promotes "
    "needs-triage), so it has its own thin workflow and must NOT be gated "
    "-- above the ceiling it is the only automated path that lowers the "
    "count, so standing it down would strand the backlog above the cap. "
    "Any OTHER wrapper that skips ./.github/workflows/_claude-scan.yml "
    "also skips the backlog ceiling and can file issues into a full queue "
    "-- either route it through the core or justify it here explicitly."
)

# --- The second exemption, and why it is a DIFFERENT kind ---------------
#
# There are exactly two exemptions and they are not the same mechanism.
# Keeping them distinct is the point: "exempt" must never become a
# generic escape hatch that the next wrapper can claim by asserting it.
#
#   groom    -- BYPASS. Not a producer at all. It never reaches the core,
#               so there is no gate to disable. Exempt because gating a
#               drain deadlocks the thing the cap exists to protect.
#   security -- ENFORCEMENT OFF. A producer that DOES route through the
#               core, keeps the gate job, keeps its `max_issues` cap, and
#               is measured on every run -- but declares
#               `enforce_backlog_ceiling: false` so depth does not
#               suppress it.
#
# The security exemption is an operator decision (2026-08-14), recorded
# because the numbers make the trade concrete: `agent-ready` stood at 163
# against a ceiling of 90, so gating security would have suppressed CVE
# and injection findings until the backlog fell by 73 issues -- an
# indefinite window with security findings unfiled, introduced by a
# backlog-hygiene change. A missed security issue costs more than a
# redundant one; that asymmetry does not hold for the other ten scans.
_SECURITY_WRAPPER = "scan-security.yml"
_CEILING_EXEMPT_WRAPPERS = frozenset({_SECURITY_WRAPPER})
_ENFORCE_INPUT = "enforce_backlog_ceiling"
_SECURITY_RATIONALE = (
    "scan-security.yml is exempt from ENFORCEMENT (not from the core): a "
    "missed security finding costs more than a redundant one, and at the "
    "measured 163-vs-90 the gate would have suppressed CVE and injection "
    "findings indefinitely. It stays a producer -- routed through the "
    "core, still capped by max_issues, still measured -- and the "
    "exemption is a single explicit `enforce_backlog_ceiling: false` at "
    "its own call site, never an implicit default. If you are adding a "
    "SECOND enforcement exemption, that is a policy change and needs the "
    "operator, not a green test."
)

# scan-security's per-run cap is what stops an exempt producer flooding
# the queue it is no longer throttled by. Pinned, so "exempt" can never
# quietly become "exempt and uncapped".
_SECURITY_MAX_ISSUES_DEFAULT = "5"

# An env key that looks like a second, independent backlog ceiling.
_CEILING_ENV_KEY_RE = re.compile(r"MAX.*QUEUE|.*CEILING")
_BARE_INT_RE = re.compile(r"^\d+$")

# Flags the one surviving depth query must carry, so "the queue" means
# the same population to every caller.
_REQUIRED_QUERY_FLAGS = ("--label", "agent-ready", "--state", "open")


# --- Parsing helpers (module-local; nothing here is shared yet) ---------


def _tokenised_run_lines(step: dict[str, Any]) -> list[list[str]]:
    """Tokenise every line of a step's ``run`` block, comment-blind.

    Uses :func:`shell_tokens`, so a command named inside a ``#`` comment
    never satisfies an assertion about the command that executes.

    Args:
        step: A parsed workflow step mapping.

    Returns:
        One token list per non-empty command line. Lines whose quoting
        only balances across a continuation cannot be attributed to a
        command and are dropped rather than raised.
    """
    run_block = step.get("run")
    if not isinstance(run_block, str):
        return []
    tokenised: list[list[str]] = []
    for raw_line in run_block.splitlines():
        try:
            tokens = shell_tokens(raw_line)
        except ValueError:
            continue
        if tokens:
            tokenised.append(tokens)
    return tokenised


def _jobs(workflow: Path) -> dict[str, dict[str, Any]]:
    """Return a workflow's jobs, keyed by job id.

    Args:
        workflow: Path to a workflow file.

    Returns:
        Only the entries that parse as mappings.
    """
    raw = load_yaml(workflow).get("jobs", {})
    if not isinstance(raw, dict):
        return {}
    return {job_id: job for job_id, job in raw.items() if isinstance(job, dict)}


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered steps of one parsed job.

    Args:
        job: A parsed job mapping.

    Returns:
        The step mappings in declaration order; empty when the job
        delegates to a reusable workflow.
    """
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


def _runs_gate_script(step: dict[str, Any]) -> bool:
    """Report whether a step invokes the backlog-gate script.

    Args:
        step: A parsed workflow step mapping.

    Returns:
        ``True`` when a tokenised ``run`` line names the script path.
    """
    return any(_GATE_SCRIPT_REL in tokens for tokens in _tokenised_run_lines(step))


def _queries_agent_ready(step: dict[str, Any]) -> bool:
    """Report whether a step runs its own ``gh issue list`` depth query.

    Args:
        step: A parsed workflow step mapping.

    Returns:
        ``True`` when a tokenised ``run`` line lists ``agent-ready``
        issues itself instead of delegating to the shared script.
    """
    return any(
        {"issue", "list", "agent-ready"} <= set(tokens)
        for tokens in _tokenised_run_lines(step)
    )


def _env_mappings(workflow: Path) -> list[dict[str, Any]]:
    """Return every parsed ``env:`` mapping in a workflow.

    Covers workflow-level, job-level and step-level ``env`` blocks.
    Parsing (rather than scanning text) is what keeps prose that mentions
    a variable from counting as a declaration of it.

    Args:
        workflow: Path to a workflow file.

    Returns:
        The ``env`` mappings found, in document order.
    """
    document = load_yaml(workflow)
    found: list[dict[str, Any]] = []
    top_level = document.get("env")
    if isinstance(top_level, dict):
        found.append(top_level)
    for job in _jobs(workflow).values():
        job_env = job.get("env")
        if isinstance(job_env, dict):
            found.append(job_env)
        for step in _job_steps(job):
            step_env = step.get("env")
            if isinstance(step_env, dict):
                found.append(step_env)
    return found


def _triggers(workflow: Path) -> dict[str, Any]:
    """Return a workflow's trigger mapping.

    PyYAML implements YAML 1.1, where the bare key ``on`` resolves to the
    boolean ``True`` rather than the string ``"on"``. Both spellings are
    checked so this helper does not silently return ``{}`` and turn a
    trigger assertion vacuous.

    Args:
        workflow: Path to a workflow file.

    Returns:
        The parsed ``on:`` mapping, or ``{}`` if it is not a mapping.
    """
    document = load_yaml(workflow)
    raw = document.get("on", document.get(True))
    return raw if isinstance(raw, dict) else {}


def _core_routed_jobs(workflow: Path) -> list[dict[str, Any]]:
    """Return the jobs of a wrapper that delegate to the scan core.

    Args:
        workflow: Path to a workflow file.

    Returns:
        Job mappings whose ``uses`` is the reusable core workflow.
    """
    return [job for job in _jobs(workflow).values() if job.get("uses") == _CORE_USES]


def _scan_wrappers() -> list[Path]:
    """Return every ``scan-*.yml`` wrapper, via glob.

    Returns:
        Sorted wrapper paths under ``.github/workflows``.
    """
    return sorted(WORKFLOWS_DIR.glob("scan-*.yml"))


def _gate_steps(workflow: Path) -> list[dict[str, Any]]:
    """Return every step of a workflow that runs the gate script.

    Args:
        workflow: Path to a workflow file.

    Returns:
        The matching step mappings.
    """
    return [step for step in workflow_steps(workflow) if _runs_gate_script(step)]


def _gate_invocations() -> list[tuple[Path, list[str]]]:
    """Return every workflow command line that runs the gate script.

    Returns:
        ``(workflow, tokens)`` pairs, one per invoking command line.
    """
    invocations: list[tuple[Path, list[str]]] = []
    for workflow in workflow_files():
        for step in workflow_steps(workflow):
            invocations.extend(
                (workflow, tokens)
                for tokens in _tokenised_run_lines(step)
                if _GATE_SCRIPT_REL in tokens
            )
    return invocations


def _names(text: str, number: int) -> bool:
    """Report whether ``text`` names ``number`` as a standalone token.

    Word boundaries keep ``90`` from matching inside ``1900``.

    Args:
        text: The text to search.
        number: The integer that must appear.

    Returns:
        ``True`` when the number appears as its own token.
    """
    return re.search(rf"\b{number}\b", text) is not None


def _summary_lines(summary: str) -> list[str]:
    """Return the non-blank lines of a job-summary fragment.

    Args:
        summary: Contents of the ``$GITHUB_STEP_SUMMARY`` file.

    Returns:
        Every line with content, stripped.
    """
    return [line.strip() for line in summary.splitlines() if line.strip()]


# --- Behavioural harness ------------------------------------------------


@dataclass(frozen=True)
class _GateResult:
    """The observable result of one real run of the gate script.

    Attributes:
        returncode: The script's exit status.
        outputs: Parsed ``key=value`` pairs from ``$GITHUB_OUTPUT``.
        summary: Text appended to ``$GITHUB_STEP_SUMMARY``.
        console: Combined stdout and stderr, for workflow-command
            annotations such as ``::error::``.
        gh_argv: Every argv line the stub ``gh`` recorded. Empty means
            the stub was never reached -- which makes any assertion
            about measurement behaviour vacuous, so tests check it.
    """

    returncode: int
    outputs: dict[str, str]
    summary: str
    console: str
    gh_argv: str


def _write_stub_gh(
    bin_dir: Path, count_output: str, exit_code: int, argv_log: Path
) -> None:
    """Install a stub ``gh`` that records its argv and prints a depth.

    Recording argv is what makes the behavioural tests non-vacuous: if
    ``PATH`` were wrong, the log stays empty and the tests say so rather
    than passing against a real (or missing) ``gh``.

    An empty ``count_output`` prints **nothing at all**, not a bare
    newline. That distinction is load-bearing and was found by mutation
    testing: a stub that emits ``"\\n"`` on the failure path lets a
    fail-open bug (``gh … || echo 0``) be caught by the *depth* guard,
    because the substituted value keeps a leading newline and fails the
    numeric check for the wrong reason. With truly empty output the
    fallback yields a clean ``0`` and the fail-open survives -- so the
    real failure path is only exercised when the stub is silent.

    Args:
        bin_dir: Directory prepended to ``PATH``.
        count_output: What the stub prints on stdout; empty prints
            nothing.
        exit_code: The status the stub exits with.
        argv_log: File the stub appends its argv to.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    emit = f"printf '%s\\n' {shlex.quote(count_output)}\n" if count_output else ""
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(argv_log))}\n'
        f"{emit}"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _parse_outputs(text: str) -> dict[str, str]:
    """Parse a ``$GITHUB_OUTPUT`` file into a mapping.

    Args:
        text: Contents of the output file.

    Returns:
        One entry per ``key=value`` line.
    """
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            parsed[key.strip()] = value.strip()
    return parsed


def _run_gate(
    tmp_path: Path,
    count_output: str,
    *,
    exit_code: int = 0,
    override: str | None = None,
    repository: str | None = None,
) -> _GateResult:
    """Run the real gate script against a stub ``gh``.

    Args:
        tmp_path: Directory to build the sandbox in (the pytest fixture,
            or a child of it when one test needs several runs).
        count_output: What the stub ``gh`` prints as the queue depth.
        exit_code: The status the stub ``gh`` exits with.
        override: Value for ``BACKLOG_CEILING``; ``None`` leaves it unset.
        repository: Value for ``GITHUB_REPOSITORY``; ``None`` unsets it.

    Returns:
        The parsed result of the run.
    """
    assert _GATE_SCRIPT.is_file(), (
        f"{_GATE_SCRIPT_REL} does not exist. It is the single home of the "
        "backlog ceiling and the single implementation of the depth check."
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    argv_log = tmp_path / "gh_argv"
    _write_stub_gh(bin_dir, count_output, exit_code, argv_log)
    output_file = tmp_path / "github_output"
    summary_file = tmp_path / "github_step_summary"
    output_file.touch()
    summary_file.touch()
    argv_log.touch()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GITHUB_OUTPUT"] = str(output_file)
    env["GITHUB_STEP_SUMMARY"] = str(summary_file)
    # Drop the ambient CI variables so a developer's shell cannot change
    # the answer: these must come only from this call.
    env.pop(_OVERRIDE_ENV, None)
    env.pop("GITHUB_REPOSITORY", None)
    if override is not None:
        env[_OVERRIDE_ENV] = override
    if repository is not None:
        env["GITHUB_REPOSITORY"] = repository

    completed = subprocess.run(
        ["bash", str(_GATE_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return _GateResult(
        returncode=completed.returncode,
        outputs=_parse_outputs(output_file.read_text(encoding="utf-8")),
        summary=summary_file.read_text(encoding="utf-8"),
        console=completed.stdout + completed.stderr,
        gh_argv=argv_log.read_text(encoding="utf-8"),
    )


# --- Wiring: the gate guards the issue writer ---------------------------


def test_gate_job_guards_the_issue_writing_job() -> None:
    """The core measures depth in a job the issue writer depends on.

    Delete this and the gate can drift *after* the ``claude-code-action``
    step, or lose the condition that consumes its verdict -- either of
    which makes the whole feature a no-op while every behavioural test in
    this module still passes.

    The guard is a job-level ``if:``, not a per-step one, so a step added
    later cannot land outside it: that is the difference between a gate
    and a convention.
    """
    jobs = _jobs(_CORE_WORKFLOW)
    assert jobs, "_claude-scan.yml has no parseable jobs"

    writer_jobs = {
        job_id: job
        for job_id, job in jobs.items()
        if any(
            str(step.get("uses", "")).startswith(_CLAUDE_ACTION)
            for step in _job_steps(job)
        )
    }
    gate_jobs = {
        job_id: job
        for job_id, job in jobs.items()
        if any(_runs_gate_script(step) for step in _job_steps(job))
    }
    assert writer_jobs, (
        f"no job in _claude-scan.yml uses {_CLAUDE_ACTION}; this test can no "
        "longer prove the gate guards the issue writer"
    )
    assert gate_jobs, (
        f"no job in _claude-scan.yml runs {_GATE_SCRIPT_REL}; the scheduled "
        "scan path is ungated and files issues at any depth"
    )
    assert not (set(writer_jobs) & set(gate_jobs)), (
        "the gate shares a job with the issue writer, so a future step could "
        "be added after the gate but before the writer with no condition"
    )

    gate_id, gate_job = next(iter(gate_jobs.items()))
    outputs = gate_job.get("outputs")
    assert isinstance(outputs, dict) and "proceed" in outputs, (
        f"job {gate_id!r} runs the gate but exports no 'proceed' output, so "
        "no other job can act on its verdict"
    )
    assert _GATE_STEP_ID in str(outputs["proceed"]), (
        f"job {gate_id!r} exports 'proceed' from {outputs['proceed']!r}; it "
        f"must read the step with id: {_GATE_STEP_ID}"
    )

    for writer_id, writer_job in writer_jobs.items():
        needs = writer_job.get("needs")
        needs_ids = [needs] if isinstance(needs, str) else list(needs or [])
        assert gate_id in needs_ids, (
            f"job {writer_id!r} files issues without needs: {gate_id}"
        )
        condition = str(writer_job.get("if", ""))
        assert f"needs.{gate_id}.outputs.proceed" in condition, (
            f"job {writer_id!r} runs regardless of the gate: its if: is {condition!r}"
        )
        # The exemption is part of the guard, not a bypass around it: the
        # condition must consult BOTH the measured verdict and the
        # per-caller enforcement switch. Dropping the switch silently
        # re-gates scan-security, which the operator ruled out; dropping
        # the verdict ungates everything.
        assert f"inputs.{_ENFORCE_INPUT}" in condition, (
            f"job {writer_id!r} ignores {_ENFORCE_INPUT}, so the security "
            f"exemption is inert and that scan is gated again. "
            f"{_SECURITY_RATIONALE}"
        )


def test_gate_step_never_interpolates_workflow_expressions_into_shell() -> None:
    """The dispatch override reaches the shell through ``env:`` only.

    Delete this and someone inlines ``${{ inputs.backlog_ceiling }}`` into
    the ``run:`` body, where a dispatch input becomes shell source text
    rather than data -- the workflow-injection class the core's existing
    ``scan_name`` charset guard was written for.
    """
    steps = [step for workflow in workflow_files() for step in _gate_steps(workflow)]
    assert steps, f"no workflow step runs {_GATE_SCRIPT_REL}"

    for step in steps:
        run_block = str(step.get("run", ""))
        assert "${{" not in run_block, (
            f"gate step {step.get('name')!r} interpolates a workflow "
            f"expression into its shell body: {run_block!r}"
        )
        step_env = step.get("env")
        assert isinstance(step_env, dict), (
            f"gate step {step.get('name')!r} has no env: block; the override "
            f"must arrive as {_OVERRIDE_ENV}"
        )


# --- One ceiling, one definition ---------------------------------------


def test_backlog_ceiling_is_defined_exactly_once() -> None:
    """The number 90 lives in exactly one place.

    Delete this and the ceiling gets copied into a workflow ``env:``
    beside the script's own constant. Two hardcoded numbers that happen
    to agree today is precisely how ``MAX_QUEUE: "80"`` in hopper.yml
    ended up guarding one path while the backlog reached 158 on another.
    """
    assert _GATE_SCRIPT.is_file(), f"{_GATE_SCRIPT_REL} does not exist"
    assignments = [
        line
        for line in non_comment_lines(_GATE_SCRIPT)
        if _CEILING_ASSIGNMENT_RE.match(line)
    ]
    assert len(assignments) == 1, (
        f"expected exactly one '{_CEILING_ASSIGNMENT}' assignment in "
        f"{_GATE_SCRIPT_REL}, found {len(assignments)}: {assignments}"
    )

    hopper_env_keys = {key for env in _env_mappings(_HOPPER_WORKFLOW) for key in env}
    hopper_run_tokens = {
        token
        for step in workflow_steps(_HOPPER_WORKFLOW)
        for tokens in _tokenised_run_lines(step)
        for token in tokens
    }
    assert "MAX_QUEUE" not in hopper_env_keys, (
        "hopper.yml still declares its own MAX_QUEUE; the ceiling now comes "
        f"from {_GATE_SCRIPT_REL}. (MIN_QUEUE, the refill trigger, stays.)"
    )
    assert not any("MAX_QUEUE" in token for token in hopper_run_tokens), (
        "hopper.yml still reads MAX_QUEUE in a run block"
    )

    duplicates = [
        (workflow.name, key, value)
        for workflow in workflow_files()
        for env in _env_mappings(workflow)
        for key, value in env.items()
        if _CEILING_ENV_KEY_RE.fullmatch(str(key))
        and _BARE_INT_RE.match(str(value).strip())
    ]
    assert not duplicates, (
        "a workflow hardcodes its own numeric backlog ceiling: "
        f"{duplicates}. The ceiling belongs to {_GATE_SCRIPT_REL} alone; a "
        "second copy silently diverges."
    )


def test_hopper_and_core_measure_depth_through_one_script() -> None:
    """Both throttled paths count the same population the same way.

    Delete this and hopper keeps its inline ``gh issue list`` while the
    core grows a second one -- two queries that can disagree about which
    issues count, so "the queue is full" means two different things
    depending on which workflow is asking.
    """
    for workflow in (_HOPPER_WORKFLOW, _CORE_WORKFLOW):
        steps = workflow_steps(workflow)
        assert steps, f"{workflow.name} has no parseable steps"
        assert any(_runs_gate_script(step) for step in steps), (
            f"{workflow.name} does not call {_GATE_SCRIPT_REL}"
        )
        own_queries = [step.get("name") for step in steps if _queries_agent_ready(step)]
        assert not own_queries, (
            f"{workflow.name} still runs its own 'gh issue list --label "
            f"agent-ready' in step(s) {own_queries}; the measurement belongs "
            f"to {_GATE_SCRIPT_REL} alone"
        )


# --- Every producer routes through the gated core -----------------------


def test_every_producer_wrapper_routes_through_the_gated_core() -> None:
    """Only the named consumer may bypass the core, and it must be named.

    Delete this and a new ``scan-*.yml`` can call the Claude action
    directly, skipping the ceiling entirely -- the exact shape of the
    original bug, where scheduled scans bypassed hopper's guard.
    """
    wrappers = _scan_wrappers()
    assert wrappers, "no scan-*.yml wrappers found; this test would be vacuous"

    # Recomputed without the glob so a broken pattern cannot shrink the
    # population under test and pass by enumerating less than exists.
    on_disk = sorted(
        entry.name
        for entry in WORKFLOWS_DIR.iterdir()
        if entry.is_file() and entry.name.startswith("scan-") and entry.suffix == ".yml"
    )
    assert on_disk, "no scan-*.yml files on disk"
    assert [path.name for path in wrappers] == on_disk

    routed = {path.name for path in wrappers if _core_routed_jobs(path)}
    bypassing = {path.name for path in wrappers} - routed

    assert routed, "no wrapper routes through the core; the gate reaches nobody"
    assert len(routed) == _CORE_ROUTED_WRAPPERS, (
        f"expected {_CORE_ROUTED_WRAPPERS} core-routed wrappers, found "
        f"{len(routed)}: {sorted(routed)}"
    )
    assert bypassing == _CONSUMER_ONLY_WRAPPERS, (
        f"wrappers bypassing the core: {sorted(bypassing)}. {_GROOM_RATIONALE}"
    )


def test_the_one_exempt_wrapper_is_still_a_consumer() -> None:
    """The exemption is justified by what groom does, not by its name.

    Delete this and ``scan-groom.yml`` can be rewritten into a producer --
    filing issues with the ``scan-issue-writer`` skill -- while keeping
    its place on the bypass allowlist, which would reopen the whole bug
    behind a green test.
    """
    prompts = [
        str(step.get("with", {}).get("prompt", ""))
        for step in workflow_steps(_GROOM_WORKFLOW)
        if isinstance(step.get("with"), dict)
    ]
    named = [prompt for prompt in prompts if prompt.strip()]
    assert named, "scan-groom.yml has no parseable prompt to classify"

    joined = "\n".join(named)
    assert "backlog-grooming" in joined, (
        "scan-groom.yml no longer invokes the backlog-grooming skill; its "
        f"bypass exemption rests on being a consumer. {_GROOM_RATIONALE}"
    )
    assert "scan-issue-writer" not in joined, (
        "scan-groom.yml now files issues with the scan-issue-writer skill, "
        f"making it a producer -- it must route through the core. "
        f"{_GROOM_RATIONALE}"
    )


def test_every_core_routed_wrapper_forwards_the_ceiling_override() -> None:
    """Each wrapper can be dispatched with a raised ceiling.

    Delete this and the override input exists on the core but nothing can
    set it, so the documented catch-up path ("raise the cap for one run")
    does not exist for any actual scan. ``gh workflow run`` rejects an
    input a wrapper does not declare, so declaring it is the whole
    mechanism.
    """
    routed = [path for path in _scan_wrappers() if _core_routed_jobs(path)]
    assert routed, "no core-routed wrappers to check; this test would be vacuous"
    assert len(routed) == _CORE_ROUTED_WRAPPERS

    for wrapper in routed:
        dispatch = _triggers(wrapper).get("workflow_dispatch")
        assert isinstance(dispatch, dict), (
            f"{wrapper.name} has no workflow_dispatch trigger to carry the override"
        )
        inputs = dispatch.get("inputs")
        assert isinstance(inputs, dict) and _OVERRIDE_INPUT in inputs, (
            f"{wrapper.name} has no {_OVERRIDE_INPUT!r} workflow_dispatch input"
        )
        for job in _core_routed_jobs(wrapper):
            with_mapping = job.get("with")
            assert isinstance(with_mapping, dict), (
                f"{wrapper.name} passes no inputs to the core"
            )
            assert _OVERRIDE_INPUT in with_mapping, (
                f"{wrapper.name} accepts a {_OVERRIDE_INPUT} input but never "
                "forwards it to the core -- the override is inert"
            )

    workflow_call = _triggers(_CORE_WORKFLOW).get("workflow_call", {})
    core_inputs = workflow_call.get("inputs") if isinstance(workflow_call, dict) else {}
    assert isinstance(core_inputs, dict) and _OVERRIDE_INPUT in core_inputs, (
        f"_claude-scan.yml declares no {_OVERRIDE_INPUT!r} workflow_call input"
    )
    assert core_inputs[_OVERRIDE_INPUT].get("required") is False, (
        f"{_OVERRIDE_INPUT} must be optional; every wrapper forwards it "
        "unconditionally and an unset dispatch input arrives empty"
    )


def test_the_security_scan_is_exempt_by_explicit_declaration() -> None:
    """Security is exempt because it says so, not because it drifted.

    Operator decision, 2026-08-14, at a measured 163 ``agent-ready``
    against a ceiling of 90: gating security would have suppressed CVE
    and injection findings until the backlog fell by 73 issues.

    Delete this and the exemption can be lost in a refactor -- security
    silently rejoins the throttle and stops filing above the cap, which
    is the failure the operator ruled out. Every other test here would
    stay green, because a gated scan is the *default* shape.
    """
    wrapper = WORKFLOWS_DIR / _SECURITY_WRAPPER
    assert wrapper.is_file(), f"{_SECURITY_WRAPPER} does not exist"

    routed_jobs = _core_routed_jobs(wrapper)
    assert routed_jobs, (
        f"{_SECURITY_WRAPPER} no longer routes through the core. Exempt "
        f"from ENFORCEMENT is not the same as detached from the pipeline. "
        f"{_SECURITY_RATIONALE}"
    )

    declared = [
        job.get("with", {}).get(_ENFORCE_INPUT)
        for job in routed_jobs
        if isinstance(job.get("with"), dict)
    ]
    assert declared, (
        f"{_SECURITY_WRAPPER} passes no {_ENFORCE_INPUT!r} to the core, so "
        f"it inherits the default and IS gated. {_SECURITY_RATIONALE}"
    )
    for value in declared:
        assert value is False, (
            f"{_SECURITY_WRAPPER} sets {_ENFORCE_INPUT}={value!r}; the "
            f"exemption requires a literal `false`. {_SECURITY_RATIONALE}"
        )


def test_the_exempt_scan_is_still_a_capped_producer() -> None:
    """Exempt from the ceiling, still bounded by its own per-run cap.

    Delete this and `scan-security.yml` can be turned into something
    else -- a consumer, an uncapped producer, a scan of a different
    name -- while keeping an exemption granted to it as a *capped
    security producer*. `max_issues` is what stops the one scan the
    ceiling no longer throttles from flooding the queue, so it is
    pinned rather than merely present.
    """
    wrapper = WORKFLOWS_DIR / _SECURITY_WRAPPER
    routed_jobs = _core_routed_jobs(wrapper)
    assert routed_jobs, f"{_SECURITY_WRAPPER} does not route through the core"

    for job in routed_jobs:
        with_mapping = job.get("with", {})
        assert with_mapping.get("scan_name") == "security", (
            f"{_SECURITY_WRAPPER} no longer runs the security scan: "
            f"scan_name={with_mapping.get('scan_name')!r}"
        )
        assert "max_issues" in with_mapping, (
            f"{_SECURITY_WRAPPER} forwards no max_issues cap. An exempt "
            "producer with no cap is unbounded above the ceiling."
        )

    dispatch = _triggers(wrapper).get("workflow_dispatch")
    assert isinstance(dispatch, dict), f"{_SECURITY_WRAPPER} lost its dispatch"
    inputs = dispatch.get("inputs", {})
    assert isinstance(inputs, dict) and "max_issues" in inputs, (
        f"{_SECURITY_WRAPPER} no longer declares a max_issues input"
    )
    assert str(inputs["max_issues"].get("default")) == _SECURITY_MAX_ISSUES_DEFAULT, (
        f"{_SECURITY_WRAPPER}'s per-run cap changed from "
        f"{_SECURITY_MAX_ISSUES_DEFAULT} to "
        f"{inputs['max_issues'].get('default')!r}. The cap is the only "
        "bound left on the one scan the ceiling does not throttle -- "
        "raising it is a policy change, not a tweak."
    )

    # The producer half: the core it routes through is the one that
    # invokes the issue writer. Parsed from the core's prompt, not
    # grepped -- every wrapper NAMES scan-issue-writer in a comment.
    core_prompts = [
        str(step.get("with", {}).get("prompt", ""))
        for step in workflow_steps(_CORE_WORKFLOW)
        if isinstance(step.get("with"), dict)
    ]
    joined = "\n".join(prompt for prompt in core_prompts if prompt.strip())
    assert joined, "_claude-scan.yml has no parseable prompt"
    assert "scan-issue-writer" in joined, (
        "the core no longer invokes scan-issue-writer, so routing through "
        "it no longer makes scan-security a producer"
    )


def test_ceiling_exemptions_are_exactly_the_two_named_ones() -> None:
    """Two exemptions, two distinct reasons, both declared here.

    This is the allowlist-creep guard. Delete it and any wrapper can
    quietly add ``enforce_backlog_ceiling: false`` and leave the
    throttle -- which is how a targeted safety carve-out becomes a
    generic escape hatch and the ceiling stops meaning anything.

    The two mechanisms are deliberately different and are asserted
    separately: groom BYPASSES the core entirely (it is not a
    producer), while security routes through the core and keeps the
    gate job but turns enforcement off.
    """
    wrappers = _scan_wrappers()
    assert wrappers, "no scan-*.yml wrappers found; this test would be vacuous"

    routed = [path for path in wrappers if _core_routed_jobs(path)]
    assert routed, "no core-routed wrappers; this test would be vacuous"
    assert len(routed) == _CORE_ROUTED_WRAPPERS

    enforcement_off = {
        path.name
        for path in routed
        for job in _core_routed_jobs(path)
        if isinstance(job.get("with"), dict)
        and job["with"].get(_ENFORCE_INPUT) is False
    }
    assert enforcement_off == _CEILING_EXEMPT_WRAPPERS, (
        f"the set of wrappers with the ceiling switched OFF is "
        f"{sorted(enforcement_off)}, expected "
        f"{sorted(_CEILING_EXEMPT_WRAPPERS)}. {_SECURITY_RATIONALE}"
    )

    bypassing = {path.name for path in wrappers} - {path.name for path in routed}
    assert bypassing == _CONSUMER_ONLY_WRAPPERS, (
        f"wrappers bypassing the core: {sorted(bypassing)}. {_GROOM_RATIONALE}"
    )
    assert not (enforcement_off & bypassing), (
        "a wrapper claims BOTH exemptions; they are different mechanisms "
        "for different reasons and nothing should need both"
    )


def test_the_core_declares_the_enforcement_switch_defaulting_to_on() -> None:
    """Gating is the default; exemption must be asked for.

    Delete this and ``enforce_backlog_ceiling`` could default to
    ``false``, which inverts the whole feature: every wrapper that does
    not mention it -- ten of the eleven -- would stop being throttled,
    and the two allowlist tests above would still pass because nobody
    declared anything.
    """
    workflow_call = _triggers(_CORE_WORKFLOW).get("workflow_call", {})
    inputs = workflow_call.get("inputs") if isinstance(workflow_call, dict) else {}
    assert isinstance(inputs, dict) and _ENFORCE_INPUT in inputs, (
        f"_claude-scan.yml declares no {_ENFORCE_INPUT!r} workflow_call input"
    )

    spec = inputs[_ENFORCE_INPUT]
    assert spec.get("type") == "boolean", (
        f"{_ENFORCE_INPUT} must be a boolean so `false` cannot be spelled "
        f"as a truthy string; got type={spec.get('type')!r}"
    )
    assert spec.get("required") is False, (
        f"{_ENFORCE_INPUT} must be optional: ten wrappers never mention it"
    )
    assert spec.get("default") is True, (
        f"{_ENFORCE_INPUT} must default to true -- gating is the default "
        f"and exemption is opt-in; got default={spec.get('default')!r}"
    )


def test_gate_script_is_executable_and_invoked_by_bare_path() -> None:
    """The invocation style and the file mode agree.

    One contract, pinned: the script carries the executable bit AND every
    workflow calls it by bare path. Delete this and the two can drift --
    a bare-path call against a non-executable file is a "Permission
    denied" that only shows up on a scheduled run nobody watches.
    """
    assert _GATE_SCRIPT.is_file(), f"{_GATE_SCRIPT_REL} does not exist"
    assert os.access(_GATE_SCRIPT, os.X_OK), (
        f"{_GATE_SCRIPT_REL} is not executable, but the workflows invoke it "
        "by bare path"
    )

    invocations = _gate_invocations()
    assert invocations, f"no workflow invokes {_GATE_SCRIPT_REL}; the gate is dead code"
    for workflow, tokens in invocations:
        assert tokens[0] == _GATE_SCRIPT_REL, (
            f"{workflow.name} invokes the gate as {tokens!r}; the pinned "
            "contract is a bare-path call (no bash/sh prefix), matching the "
            "executable bit asserted above"
        )


# --- Behaviour of the script itself -------------------------------------


def test_standing_down_succeeds_and_reports_count_and_ceiling(
    tmp_path: Path,
) -> None:
    """A full queue is a clean stand-down that shows its arithmetic.

    Delete this and standing down could exit non-zero -- a red nightly
    workflow trains everyone to ignore the workflow -- or could stand
    down without saying how deep the queue was or what the cap is,
    leaving no way to tell a working gate from a stuck one.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    result = _run_gate(tmp_path, str(_MEASURED_DEPTH))

    assert result.gh_argv.strip(), (
        "the stub gh was never called, so this test proves nothing about "
        "measurement; PATH is wrong"
    )
    assert result.returncode == 0, (
        "standing down is a SUCCESS, not a failure; a red scheduled run "
        f"teaches the team to ignore it. console: {result.console}"
    )
    assert result.outputs.get("proceed") == "false"
    assert result.outputs.get("count") == str(_MEASURED_DEPTH)
    assert result.outputs.get("ceiling") == str(_DEFAULT_CEILING)
    assert _names(result.summary, _MEASURED_DEPTH), (
        f"job summary never names the measured depth: {result.summary!r}"
    )
    assert _names(result.summary, _DEFAULT_CEILING), (
        f"job summary never names the ceiling: {result.summary!r}"
    )
    assert len(_summary_lines(result.summary)) == 1, (
        f"expected one summary line, got {_summary_lines(result.summary)}"
    )


def test_the_ceiling_is_inclusive_at_the_boundary(tmp_path: Path) -> None:
    """A queue exactly at the cap stands down.

    Delete this and ``-ge`` can slip to ``-gt``, so the ceiling means
    "91" for the scans while the guard hopper used to carry meant "at or
    above". An off-by-one in a throttle is invisible until it is a trend.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    result = _run_gate(tmp_path, str(_DEFAULT_CEILING))

    assert result.gh_argv.strip(), "the stub gh was never called"
    assert result.returncode == 0, result.console
    assert result.outputs.get("proceed") == "false", (
        "at exactly the ceiling the queue is full, not eligible: the "
        "comparison is >=, matching the guard hopper used to carry"
    )


def test_under_the_ceiling_the_scan_proceeds(tmp_path: Path) -> None:
    """A shallow queue is allowed to produce.

    Delete this and a gate that stands down unconditionally would still
    pass every stand-down test -- the pipeline silently stops filing
    issues forever, and nothing goes red.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    shallow = 12
    result = _run_gate(tmp_path, str(shallow))

    assert result.gh_argv.strip(), "the stub gh was never called"
    assert result.returncode == 0, result.console
    assert result.outputs.get("proceed") == "true"
    assert result.outputs.get("count") == str(shallow)
    assert result.outputs.get("ceiling") == str(_DEFAULT_CEILING)
    assert _names(result.summary, shallow), result.summary
    assert _names(result.summary, _DEFAULT_CEILING), result.summary
    assert len(_summary_lines(result.summary)) == 1, (
        f"expected one summary line, got {_summary_lines(result.summary)}"
    )


def test_the_depth_query_names_the_population_and_the_repository(
    tmp_path: Path,
) -> None:
    """The one surviving query counts open agent-ready issues, here.

    Delete this and the shared script can drift to counting *all* open
    issues, or to whatever repository the runner happens to sit in -- and
    every other behavioural test would still pass, because they only stub
    the number that comes back.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    repository = "Geoffe-Ga/Creek-Vault"
    result = _run_gate(tmp_path, "12", repository=repository)

    argv = result.gh_argv
    assert argv.strip(), "the stub gh was never called"
    assert "issue list" in argv, f"the script no longer lists issues: {argv!r}"
    for flag in _REQUIRED_QUERY_FLAGS:
        assert flag in argv, (
            f"the depth query dropped {flag!r}, so it no longer measures the "
            f"population hopper measured: {argv!r}"
        )
    assert repository in argv, (
        "the query does not name $GITHUB_REPOSITORY, so it depends on the "
        f"runner's working directory: {argv!r}"
    )


def test_ceiling_override_is_honoured_and_validated(tmp_path: Path) -> None:
    """The dispatch override raises the cap, or fails loudly.

    Delete this and the override becomes decoration: read but ignored
    (so a deliberate catch-up run still stands down), or silently
    defaulted on a typo (so ``backlog_ceiling: 9O`` quietly reverts to 90
    and the operator believes the cap was raised).

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    raised = 200
    lifted = _run_gate(tmp_path / "raised", str(_MEASURED_DEPTH), override=str(raised))
    assert lifted.gh_argv.strip(), "the stub gh was never called"
    assert lifted.returncode == 0, lifted.console
    assert lifted.outputs.get("proceed") == "true"
    assert lifted.outputs.get("ceiling") == str(raised)
    assert _names(lifted.summary, raised), lifted.summary
    assert not _names(lifted.summary, _DEFAULT_CEILING), (
        "the override was read but the default ceiling is still reported: "
        f"{lifted.summary!r}"
    )

    typo = _run_gate(tmp_path / "typo", str(_MEASURED_DEPTH), override="not-a-number")
    assert typo.returncode != 0, (
        "a non-numeric override must fail loudly, never fall back to the "
        f"default. outputs={typo.outputs} summary={typo.summary!r}"
    )
    assert "::error::" in typo.console, typo.console

    empty = _run_gate(tmp_path / "empty", str(_MEASURED_DEPTH), override="")
    assert empty.returncode == 0, empty.console
    assert empty.outputs.get("ceiling") == str(_DEFAULT_CEILING), (
        "an unset workflow_dispatch input arrives as the empty string and "
        "must mean 'use the default', not 'no ceiling'"
    )
    assert empty.outputs.get("proceed") == "false"


def test_ceiling_override_cannot_inject_shell(tmp_path: Path) -> None:
    """A hostile override is rejected before it can be evaluated.

    The override is the one value that crosses from a ``workflow_dispatch``
    input into the gate's shell. Delete this and validation can weaken to
    a substring test, or the value can reach an arithmetic context that
    evaluates it -- turning "raise the cap for one run" into arbitrary
    command execution on a runner holding ``issues: write``.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    marker = tmp_path / "pwned"
    result = _run_gate(
        tmp_path / "run",
        str(_MEASURED_DEPTH),
        override=f"90; touch {marker}",
    )

    assert result.returncode != 0, (
        f"a shell-metacharacter override was accepted: {result.outputs}"
    )
    assert "::error::" in result.console, result.console
    assert not marker.exists(), (
        "the override reached a shell evaluation context and ran a command"
    )


def test_unmeasurable_depth_fails_loudly(tmp_path: Path) -> None:
    """When ``gh`` cannot answer, the run goes red rather than guessing.

    The two silent alternatives are both worse. Failing *open* files
    issues into a queue nobody measured -- the outcome the ceiling exists
    to prevent. Failing *closed* quietly would disable every producer
    indefinitely on a broken token, behind a warning nobody reads. A
    measurement failure is a genuine error, so it is reported as one;
    that is different from the routine stand-down, which is a success.

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    result = _run_gate(tmp_path, "", exit_code=1)

    assert result.gh_argv.strip(), "the stub gh was never called"
    assert result.returncode != 0, (
        "an unmeasurable queue must not be reported as a clean verdict: "
        f"outputs={result.outputs} console={result.console!r}"
    )
    assert "::error::" in result.console, result.console
    assert result.outputs.get("proceed") != "true", (
        "failing OPEN here files issues into an unmeasured backlog"
    )


def test_non_numeric_depth_fails_loudly(tmp_path: Path) -> None:
    """A successful ``gh`` call that returns nonsense is still a failure.

    ``gh`` exits 0 while printing ``null`` when a ``--jq`` filter misses.
    Delete this and that ``null`` flows into the comparison, where bash
    treats it as an error only by luck of the operator used -- or worse,
    as zero, which reads as "the queue is empty, file away".

    Args:
        tmp_path: pytest per-test temporary directory.
    """
    result = _run_gate(tmp_path, "null")

    assert result.gh_argv.strip(), "the stub gh was never called"
    assert result.returncode != 0, (
        f"'null' was accepted as a queue depth: outputs={result.outputs}"
    )
    assert "::error::" in result.console, result.console
    assert result.outputs.get("proceed") != "true"
