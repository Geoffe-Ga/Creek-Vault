"""The contract-fuzz gate builds the command it claims to build.

``scripts/dast-contract-fuzz.sh`` is executed here with a recording stub named
``schemathesis`` first on ``PATH``, and the assertions are on the argv the stub
received -- not on the script's text, which cannot tell a live flag from a
commented-out one. Whether the real fuzzer then *fails* on a real violation is
the job of ``tests/dast_planted_bug_check.py``, which the DAST workflow runs
against the pinned tool.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import pytest

from creek_mcp.api.models import CONTRACT_MINOR
from creek_mcp.api.openapi import build_openapi

_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCRIPT: Final[Path] = _ROOT / "scripts" / "dast-contract-fuzz.sh"
_CONFIG: Final[Path] = _ROOT / "scripts" / "dast-schemathesis.toml"

_TOKEN: Final[str] = "dast-token-for-the-recording-stub-0000000000"
_BASE_URL: Final[str] = "http://127.0.0.1:8823"

_STUB: Final[str] = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "$RECORD_FILE"
exit "${STUB_EXIT:-0}"
"""

_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options"}
)


def _published_operations() -> set[str]:
    """Return every ``METHOD /path`` the generated document publishes."""
    paths: dict[str, dict[str, object]] = build_openapi()["paths"]
    return {
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        for method in operations
        if method in _HTTP_METHODS
    }


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment whose ``schemathesis`` records its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "schemathesis"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    document = tmp_path / "openapi.json"
    document.write_text('{"openapi": "3.1.0"}', encoding="utf-8")
    python_dir = str(Path(sys.executable).parent)
    return {
        **os.environ,
        "PATH": os.pathsep.join([str(bin_dir), python_dir, os.environ.get("PATH", "")]),
        "RECORD_FILE": str(tmp_path / "argv"),
        "BASE_URL": _BASE_URL,
        "DAST_TOKEN": _TOKEN,
        "OPENAPI_DOC": str(document),
        "REPORT_DIR": str(tmp_path / "report"),
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script under *env* and capture what it printed."""
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _argv(env: dict[str, str]) -> list[str]:
    """Run the script and return the argv the stub recorded."""
    completed = _run(env)
    assert completed.returncode == 0, completed.stderr
    recorded = Path(env["RECORD_FILE"]).read_text(encoding="utf-8")
    return recorded.split("\0")[:-1]


def _values(argv: list[str], flag: str) -> list[str]:
    """Return every value passed after *flag* in *argv*."""
    return [argv[index + 1] for index, part in enumerate(argv) if part == flag]


def test_no_operation_is_excluded_from_the_run(stub_env: dict[str, str]) -> None:
    """Every published operation is fuzzed; a 503 refusal is handled per check."""
    argv = _argv(stub_env)

    assert not [part for part in argv if part.startswith("--exclude")]


def test_the_fuzzer_reads_the_named_document_against_the_named_instance(
    stub_env: dict[str, str],
) -> None:
    """The document and the target are the two things a wrong default would swap."""
    argv = _argv(stub_env)

    assert argv[:4] == [
        "--config-file",
        str(_CONFIG),
        "run",
        stub_env["OPENAPI_DOC"],
    ]
    assert _values(argv, "--url") == [_BASE_URL]


def test_the_checks_are_named_rather_than_all(stub_env: dict[str, str]) -> None:
    """``--checks all`` would change meaning with every upgrade."""
    checks = _values(_argv(stub_env), "--checks")

    assert checks == [
        "not_a_server_error,content_type_conformance,"
        "response_schema_conformance,status_code_conformance"
    ]


def test_the_bearer_token_and_the_contract_minor_are_both_sent(
    stub_env: dict[str, str],
) -> None:
    """Without either, every case is refused at a gate and no handler runs."""
    headers = _values(_argv(stub_env), "--header")

    assert f"Authorization: Bearer {_TOKEN}" in headers
    assert f"X-Creek-Contract-Version: {CONTRACT_MINOR}" in headers


def test_the_run_is_replayable_and_reported(stub_env: dict[str, str]) -> None:
    """A fixed seed is what makes a failure reproducible byte for byte."""
    argv = _argv(stub_env)

    assert _values(argv, "--seed") == ["1337"]
    assert _values(argv, "--phases") == ["examples,fuzzing"]
    assert _values(argv, "--report") == ["junit"]
    assert _values(argv, "--report-dir") == [stub_env["REPORT_DIR"]]


def test_the_fuzzer_verdict_is_the_script_verdict(stub_env: dict[str, str]) -> None:
    """``exec`` means no trailing command can swallow a failing run."""
    completed = _run({**stub_env, "STUB_EXIT": "1"})

    assert completed.returncode == 1


def test_the_script_takes_no_arguments(stub_env: dict[str, str]) -> None:
    """An argument is a way to quietly stop operations being fuzzed."""
    completed = _run(stub_env, "--exclude-method", "GET")

    assert completed.returncode == 2
    assert not Path(stub_env["RECORD_FILE"]).exists()


@pytest.mark.parametrize(
    "variable", ["BASE_URL", "DAST_TOKEN", "OPENAPI_DOC", "REPORT_DIR"]
)
def test_every_input_is_required(stub_env: dict[str, str], variable: str) -> None:
    """Every default here would be a way to fuzz the wrong thing and pass."""
    env = {key: value for key, value in stub_env.items() if key != variable}

    completed = _run(env)

    assert completed.returncode != 0
    assert variable in completed.stderr
    assert not Path(stub_env["RECORD_FILE"]).exists()


def test_an_empty_document_is_refused(stub_env: dict[str, str]) -> None:
    """A document with no operations fuzzes nothing and passes."""
    Path(stub_env["OPENAPI_DOC"]).write_text("", encoding="utf-8")

    completed = _run(stub_env)

    assert completed.returncode == 2
    assert "OPENAPI_DOC" in completed.stderr


def _allowances() -> list[dict[str, object]]:
    """Return the per-operation blocks of the Schemathesis config."""
    config = tomllib.loads(_CONFIG.read_text(encoding="utf-8"))
    operations: list[dict[str, object]] = config.get("operations", [])
    assert operations, "the config parsed to no operation blocks"
    return operations


def _expected_statuses(block: dict[str, object]) -> list[str]:
    """Return the server-error check's accepted statuses for one block."""
    checks = block["checks"]
    assert isinstance(checks, dict)
    statuses: list[str] = checks["not_a_server_error"]["expected-statuses"]
    return statuses


def test_every_503_allowance_names_a_published_operation() -> None:
    """A renamed route must turn red, not leave an allowance aimed at nothing."""
    names = {block["include-name"] for block in _allowances()}

    assert names <= _published_operations()


def test_no_allowance_admits_any_server_error_but_503() -> None:
    """A 500 is never a published refusal; only the documented 503 may pass."""
    for block in _allowances():
        server_errors = [
            status for status in _expected_statuses(block) if status.startswith("5")
        ]
        assert server_errors == ["503"], block


def test_the_allowances_stay_a_minority_of_the_api() -> None:
    """A list that grows to the API is a server-error check that checks nothing."""
    assert len(_allowances()) * 3 <= len(_published_operations())
