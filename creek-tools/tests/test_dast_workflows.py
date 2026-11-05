"""The two DAST workflows keep the promises their headers make.

``dast-contract.yml`` (every pull request) and ``dast-deep.yml`` (nightly ZAP)
each defend against a specific way of going quietly green or quietly unsafe:
a deep scan reachable from a fork, an attack aimed at a host the job did not
start, a ZAP run whose warnings were all waved through with ``-I``, a scan whose
credential never reached ZAP, a gate whose fuzzer was never watched fail. Each
test below pins one of those promises against the parsed workflow, so a later
edit that breaks one turns red here rather than in an incident.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest

from tests.shell_command_support import WORKFLOWS_DIR, load_yaml

_CREEK_TOOLS: Final[Path] = Path(__file__).resolve().parents[1]
_REPO_ROOT: Final[Path] = _CREEK_TOOLS.parent
_CONTRACT: Final[Path] = WORKFLOWS_DIR / "dast-contract.yml"
_DEEP: Final[Path] = WORKFLOWS_DIR / "dast-deep.yml"
_RULES: Final[Path] = _CREEK_TOOLS / "scripts" / "dast-zap-rules.tsv"
_PLANTED: Final[Path] = _CREEK_TOOLS / "tests" / "dast_planted_bug_check.py"

_LOOPBACK: Final[frozenset[str]] = frozenset({"127.0.0.1"})
_MAX_RULE_SUPPRESSIONS: Final[int] = 6
_DISPOSITIONS: Final[frozenset[str]] = frozenset({"IGNORE", "WARN", "FAIL"})

# Rules that must stay armed: the passive header checks are the
# attacker's-eye view of what /v1 actually sends, and 100000 is ZAP's
# "a server error was returned", the one rule that sees a 500.
_ARMED_RULES: Final[frozenset[str]] = frozenset(
    {"10020", "10021", "10036", "10037", "10038", "100000"}
)


def _triggers(workflow: Path) -> dict[str, Any]:
    """Return a workflow's ``on:`` block.

    PyYAML reads the bare key ``on`` as the boolean ``True`` (YAML 1.1), so
    both spellings are accepted.
    """
    document = load_yaml(workflow)
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), f"{workflow.name} has no on: mapping"
    return triggers


def _steps(workflow: Path, job: str) -> list[dict[str, Any]]:
    """Return the steps of one job."""
    steps: list[dict[str, Any]] = load_yaml(workflow)["jobs"][job]["steps"]
    return steps


def _step(workflow: Path, job: str, name: str) -> dict[str, Any]:
    """Return the one step called *name*."""
    matches = [step for step in _steps(workflow, job) if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r} in {workflow.name}"
    return matches[0]


def _index(workflow: Path, job: str, name: str) -> int:
    """Return the position of the step called *name*."""
    return _steps(workflow, job).index(_step(workflow, job, name))


def _zap(job_step: dict[str, Any]) -> dict[str, Any]:
    """Return the ``with:`` inputs of the ZAP step."""
    inputs: dict[str, Any] = job_step["with"]
    return inputs


def _rule_lines() -> list[str]:
    """Return the non-comment, non-blank lines of the ZAP rules file."""
    return [
        line
        for line in _RULES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# --- dast-deep.yml -------------------------------------------------------


def test_the_deep_scan_is_never_reachable_from_a_pull_request() -> None:
    """A fork could otherwise aim the attack container with code it wrote."""
    assert set(_triggers(_DEEP)) == {"schedule", "workflow_dispatch"}


def test_the_deep_scan_only_ever_names_the_loopback_instance() -> None:
    """A deployed vault holds people's journals; scanning one is an incident."""
    text = _DEEP.read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([^/:\s\"'$]+)", text))

    assert hosts, "no URL found at all -- this check would be vacuous"
    assert hosts <= _LOOPBACK, hosts


def test_the_deep_scan_never_passes_every_warning_at_once() -> None:
    """``-I`` is every suppression in the rules file, un-reviewed."""
    options = _zap(_step(_DEEP, "zap-api-scan", "ZAP API scan"))["cmd_options"]

    assert not re.search(r"(^|\s)-I(\s|$)", options), options


def test_the_attack_phase_has_a_bound_zap_actually_reads() -> None:
    """``-T`` does not bound the active scan; only this startup config does."""
    options = _zap(_step(_DEEP, "zap-api-scan", "ZAP API scan"))["cmd_options"]

    assert "scanner.maxScanDurationInMins=" in options


def test_the_contract_minor_header_is_read_from_the_code() -> None:
    """A hardcoded minor would turn the whole scan into one 409 after a bump."""
    options = _zap(_step(_DEEP, "zap-api-scan", "ZAP API scan"))["cmd_options"]

    assert "matchstr=X-Creek-Contract-Version" in options
    assert "replacement=${{ env.CONTRACT_MINOR }}" in options


def test_zap_reports_rather_than_blocks_and_files_nothing_itself() -> None:
    """Findings go to the Security tab; the action's issue writer stays off."""
    inputs = _zap(_step(_DEEP, "zap-api-scan", "ZAP API scan"))

    assert inputs["fail_action"] is False
    assert inputs["allow_issue_writing"] is False


def test_the_rules_file_the_scan_names_is_the_one_in_the_repository() -> None:
    """A rules path that resolves nowhere is silently no rules at all."""
    inputs = _zap(_step(_DEEP, "zap-api-scan", "ZAP API scan"))

    assert (_REPO_ROOT / inputs["rules_file_name"]).resolve() == _RULES


def test_the_attack_job_holds_no_issue_scope() -> None:
    """Only the reporting job, which runs no attack container, may file."""
    document = load_yaml(_DEEP)

    assert document["permissions"] == {"contents": "read", "security-events": "write"}
    assert "permissions" not in document["jobs"]["zap-api-scan"]
    assert document["jobs"]["report-failure"]["permissions"]["issues"] == "write"


def test_evidence_is_checked_before_anything_is_published() -> None:
    """Publishing a SARIF from a scan that reached nothing is the silent green."""
    job = "zap-api-scan"
    scan = _index(_DEEP, job, "ZAP API scan")
    evidence = _index(_DEEP, job, "Prove the scan reached authenticated code")
    convert = _index(_DEEP, job, "Convert the ZAP report to SARIF")
    publish = _index(_DEEP, job, "Publish the findings to code scanning")

    assert scan < evidence < convert < publish


def test_evidence_reads_only_the_traffic_zap_caused() -> None:
    """The readiness probe is an authenticated /v1/wheel request of its own."""
    run = _step(_DEEP, "zap-api-scan", "Prove the scan reached authenticated code")[
        "run"
    ]

    assert "API_LOG_MARK" in run
    assert "--probe-path /v1/wheel" in run


def test_a_harness_failure_reaches_a_tracking_issue() -> None:
    """A red nightly that nobody is told about is a disarmed scan."""
    reporter = load_yaml(_DEEP)["jobs"]["report-failure"]

    assert reporter["needs"] == "zap-api-scan"
    assert reporter["if"] == "failure()"


# --- dast-contract.yml ---------------------------------------------------


def test_the_contract_gate_runs_on_pull_requests_but_never_on_target() -> None:
    """``pull_request_target`` would run branch code holding write scope."""
    triggers = _triggers(_CONTRACT)

    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers
    assert triggers["push"]["branches"] == ["main"]


def test_push_and_pull_request_watch_the_same_paths() -> None:
    """A path watched on one trigger only is a change the gate sometimes misses."""
    triggers = _triggers(_CONTRACT)

    assert triggers["push"]["paths"] == triggers["pull_request"]["paths"]


@pytest.mark.parametrize(
    "path",
    [
        "creek-tools/tests/dast_planted_bug_check.py",
        "creek-tools/requirements-dast.txt",
        "creek-tools/uv.lock",
        ".github/workflows/dast-contract.yml",
    ],
)
def test_every_input_of_the_gate_is_watched_and_exists(path: str) -> None:
    """A filter naming a moved file stops the gate running on its own changes."""
    assert path in _triggers(_CONTRACT)["pull_request"]["paths"]
    assert (_REPO_ROOT / path).is_file()


def test_the_fuzzer_is_watched_failing_before_it_reports() -> None:
    """A gate that has never been watched fail is not known to work."""
    job = "contract-fuzz"
    planted = _step(_CONTRACT, job, "Prove the fuzzer catches a planted bug")

    assert "tests/dast_planted_bug_check.py" in planted["run"]
    assert _index(_CONTRACT, job, "Prove the fuzzer catches a planted bug") < _index(
        _CONTRACT, job, "Contract fuzz"
    )


def test_the_fuzz_step_is_the_script_and_nothing_else() -> None:
    """A trailing command would become the step's exit code."""
    run = _step(_CONTRACT, "contract-fuzz", "Contract fuzz")["run"]

    assert run.strip() == "scripts/dast-contract-fuzz.sh"


def test_the_planted_bug_check_is_not_collected_by_the_unit_lane() -> None:
    """It needs a tool the locked dev environment deliberately lacks."""
    assert _PLANTED.is_file()
    assert not _PLANTED.name.startswith("test_")


# --- scripts/dast-zap-rules.tsv -----------------------------------------


def test_every_rule_line_parses_the_way_zap_parses_it() -> None:
    """A space-separated line is one field to ZAP and silently does nothing."""
    lines = _rule_lines()

    assert lines, "the rules file holds no rules -- this check would be vacuous"
    for line in lines:
        fields = line.split("\t")
        assert len(fields) == 3, line
        rule_id, disposition, reason = fields
        assert rule_id.isdigit(), line
        assert disposition in _DISPOSITIONS, line
        assert reason.startswith("(") and reason.endswith(")") and len(reason) > 20, (
            line
        )


def test_the_suppressions_stay_few() -> None:
    """Every suppression is a blind spot; a long list is a scan that sees little."""
    assert len(_rule_lines()) <= _MAX_RULE_SUPPRESSIONS


def test_the_header_and_server_error_rules_stay_armed() -> None:
    """Suppressing these would hide exactly what a deployed vault leaks."""
    listed = {line.split("\t")[0] for line in _rule_lines()}

    assert not listed & _ARMED_RULES
