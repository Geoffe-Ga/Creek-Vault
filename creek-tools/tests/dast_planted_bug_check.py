"""End-to-end proof that the contract-fuzz gate can actually catch a violation.

``tests/test_dast_contract_fuzz_script.py`` proves the *command* is built
correctly by running the script under a recording stub. None of that proves the
fuzzer, once it really runs, fails on anything. A job that fuzzes zero
operations -- a document that parses to nothing, a version bump that changes
what a flag means, a check name that quietly stops resolving -- looks exactly
like a healthy gate: exit 0, no findings, nobody looks again.

So this module runs the real ``scripts/dast-contract-fuzz.sh``, with the real
pinned Schemathesis from ``requirements-dast.txt``, against a real Starlette
application served over a real socket, and plants three bugs in it:

* ``GET /boom`` raises -- a ``500``;
* ``GET /liar`` returns a body contradicting the schema it publishes;
* ``GET /v1/wheel`` answers ``503``, which ``scripts/dast-schemathesis.toml``
  does **not** allow for that operation.

The run must fail and name all three. Then the same harness serves the repaired
twin, which also answers ``GET /v1/jobs/{job_id}`` with the ``503`` the config
*does* allow, and that run must pass with every operation tested. Together they
prove the gate catches a server error and a schema lie, and that its 503
allowances are scoped to the operations that name them.

**Why this file is not named ``test_*.py``.** It needs a tool deliberately kept
out of the locked dev environment (see ``requirements-dast.txt``), and the
house rules forbid a skip. So it is not collected by the default suite at all;
``.github/workflows/dast-contract.yml`` runs it by path, in the one environment
that installs the tool, and a missing executable there is a red job.

Run it locally with::

    uv pip install -r requirements-dast.txt
    python -m pytest tests/dast_planted_bug_check.py --no-cov
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCRIPT: Final[Path] = _ROOT / "scripts" / "dast-contract-fuzz.sh"

_EXECUTABLE: Final[str] = "schemathesis"

_BOOT_TIMEOUT_SECONDS: Final[float] = 30.0
_BOOT_POLL_SECONDS: Final[float] = 0.2
_FUZZ_TIMEOUT_SECONDS: Final[int] = 300
_SHUTDOWN_TIMEOUT_SECONDS: Final[int] = 10

# A sentinel, searched for afterwards: the JUnit report is uploaded as a build
# artifact, and Schemathesis prints a reproduction `curl` for every finding. If
# it ever stops filtering the header it was handed, this string lands in a
# public artifact and the assertion below turns red.
_SENTINEL_TOKEN: Final[str] = "dast-sentinel-token-7c1e4a9b"  # pragma: allowlist secret

# What Schemathesis writes in place of a header value it was given. Asserting
# the marker is present proves the header was sent, so the token's absence is
# filtering rather than a run that never authenticated.
_FILTERED_HEADER: Final[str] = "Authorization: [Filtered]"

_SERVER_ERROR_TITLE: Final[str] = "Server error"
_SCHEMA_VIOLATION_TITLE: Final[str] = "Response violates schema"

_COUNTED: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}

_APP_SOURCE: Final[str] = """
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

BROKEN = {broken}


async def healthy(request):
    return JSONResponse({{"count": 1}})


async def boom(request):
    raise RuntimeError("planted server error")


async def liar(request):
    if BROKEN:
        return JSONResponse({{"count": "not-a-number"}})
    return JSONResponse({{"count": 2}})


async def wheel(request):
    if BROKEN:
        return JSONResponse({{"code": "temporarily_unavailable"}}, status_code=503)
    return JSONResponse({{"count": 3}})


async def job(request):
    return JSONResponse({{"code": "unavailable"}}, status_code=503)


routes = [
    Route("/healthy", healthy),
    Route("/liar", liar),
    Route("/v1/wheel", wheel),
    Route("/v1/jobs/{{job_id}}", job),
]
if BROKEN:
    routes.append(Route("/boom", boom))
app = Starlette(routes=routes)
"""


def _operation(*, refusal: bool = False) -> dict[str, object]:
    """Return one published GET operation answering the counted body."""
    responses: dict[str, object] = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": _COUNTED}},
        }
    }
    if refusal:
        responses["503"] = {
            "description": "the published refusal",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    return {"get": {"responses": responses}}


def _document(*, broken: bool) -> dict[str, object]:
    """Return the OpenAPI document the planted app publishes."""
    job = _operation(refusal=True)
    job["parameters"] = [
        {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    paths: dict[str, object] = {
        "/healthy": _operation(),
        "/liar": _operation(),
        # Declared, so status-code conformance accepts it and only the
        # server-error check -- the one the config's allowances relax -- can
        # catch it. Undeclared, a different check would report it and the
        # scoping proof below would pass whatever the config allowed.
        "/v1/wheel": _operation(refusal=True),
        "/v1/jobs/{job_id}": job,
    }
    if broken:
        paths["/boom"] = _operation()
    return {
        "openapi": "3.1.0",
        "info": {"title": "planted", "version": "1"},
        "paths": paths,
    }


def _free_port() -> int:
    """Return a loopback port nothing is listening on right now."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def _served(tmp_path: Path, *, broken: bool) -> Iterator[str]:
    """Serve the planted app on loopback for the duration of the block."""
    app_dir = tmp_path / ("broken" if broken else "fixed")
    app_dir.mkdir()
    (app_dir / "planted_app.py").write_text(
        _APP_SOURCE.format(broken=broken), encoding="utf-8"
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "planted_app:app",
            "--app-dir",
            str(app_dir),
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
    )
    try:
        deadline = time.monotonic() + _BOOT_TIMEOUT_SECONDS
        while True:
            try:
                httpx.get(f"{base_url}/healthy", timeout=1.0)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    pytest.fail("the planted app never started listening")
                time.sleep(_BOOT_POLL_SECONDS)
        yield base_url
    finally:
        server.terminate()
        server.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)


def _fuzz(tmp_path: Path, base_url: str, *, broken: bool) -> tuple[int, str, Path]:
    """Run the real gate against *base_url* and return its verdict and output."""
    label = "broken" if broken else "fixed"
    document = tmp_path / f"{label}-openapi.json"
    document.write_text(json.dumps(_document(broken=broken)), encoding="utf-8")
    report_dir = tmp_path / f"{label}-report"
    completed = subprocess.run(
        ["bash", str(_SCRIPT)],
        env={
            **os.environ,
            "BASE_URL": base_url,
            "DAST_TOKEN": _SENTINEL_TOKEN,
            "OPENAPI_DOC": str(document),
            "REPORT_DIR": str(report_dir),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=_FUZZ_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr, report_dir


def _findings_for(output: str, operation: str) -> str:
    """Return the block of fuzzer output reporting failures for *operation*.

    Schemathesis heads each failing operation with a rule of underscores around
    its name; the block runs to the next such rule.
    """
    blocks = re.split(r"^_{3,} (.+?) _{3,}$", output, flags=re.MULTILINE)
    by_name = dict(zip(blocks[1::2], blocks[2::2], strict=True))
    assert operation in by_name, f"{operation} was not reported:\n{output}"
    return by_name[operation]


def _tested(output: str) -> int:
    """Return how many operations Schemathesis reports having tested."""
    match = re.search(r"Tested:\s*(\d+)", output)
    assert match is not None, f"no 'Tested:' line in the fuzzer's output:\n{output}"
    return int(match[1])


@pytest.fixture(autouse=True)
def _require_the_pinned_fuzzer() -> None:
    """Fail -- never skip -- when the pinned executable is not installed."""
    if shutil.which(_EXECUTABLE) is None:
        pytest.fail(
            f"`{_EXECUTABLE}` is not on PATH. Install the pinned version with "
            "`uv pip install -r requirements-dast.txt`; this check exists to "
            "prove that exact pin catches a planted bug."
        )


def test_the_gate_fails_on_a_server_error_a_schema_lie_and_an_unallowed_503(
    tmp_path: Path,
) -> None:
    """A gate that has never been watched fail is not known to work."""
    with _served(tmp_path, broken=True) as base_url:
        code, output, report_dir = _fuzz(tmp_path, base_url, broken=True)

    assert code != 0, output
    assert _SERVER_ERROR_TITLE in _findings_for(output, "GET /boom")
    assert _SCHEMA_VIOLATION_TITLE in _findings_for(output, "GET /liar")
    # The scoping proof: the wheel declares its 503, so only the server-error
    # check can flag it, and only because the config allows 503 elsewhere.
    assert _SERVER_ERROR_TITLE in _findings_for(output, "GET /v1/wheel")
    report = "".join(
        path.read_text(encoding="utf-8") for path in report_dir.glob("*.xml")
    )
    assert report, "the gate wrote no JUnit report"
    assert _SENTINEL_TOKEN not in report
    assert _SENTINEL_TOKEN not in output
    assert _FILTERED_HEADER in output


def test_the_repaired_twin_passes_with_every_operation_tested(tmp_path: Path) -> None:
    """Passing and fuzzing nothing are the two things this gate tells apart."""
    with _served(tmp_path, broken=False) as base_url:
        code, output, _ = _fuzz(tmp_path, base_url, broken=False)

    paths = _document(broken=False)["paths"]
    assert isinstance(paths, dict)
    assert code == 0, output
    assert _tested(output) == len(paths)
