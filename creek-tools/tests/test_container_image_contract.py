"""Static image and CI promises for the one-vault runtime (#1772)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import yaml

from creek.ingest.gdrive import _EXTENSION_ROUTES
from creek_mcp.api.models import RequestKeyProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
CONTRACT_SCRIPT = REPO_ROOT / "creek-tools" / "scripts" / "container-contract.sh"
DOC = REPO_ROOT / "creek-tools" / "docs" / "container-runtime.md"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_base_image_is_version_and_digest_pinned() -> None:
    """Measured-image work starts from one immutable upstream index."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(
        r"^FROM python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}$", text, re.M
    )
    assert match is not None
    assert "uv sync --locked --no-dev --no-install-project" in text
    assert "--all-extras" not in text
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in text


_ROUTE_EXTRAS: Final[dict[str, str]] = {
    "document": "documents",
    "spreadsheet": "spreadsheets",
    "presentation": "presentations",
}
"""The ``pyproject`` extra each upload ingestor route imports its parser from."""

_ROUTES_WITHOUT_EXTRA: Final[frozenset[str]] = frozenset({"markdown"})
"""Routes whose ingestor parses with the standard library alone."""

_ROUTES_DEGRADING_WITHOUT_EXTRA: Final[frozenset[str]] = frozenset({"image"})
"""Routes deliberately left without their extra in the image.

``image`` needs the ``ocr`` extra *and* the ``tesseract`` / ``poppler`` system
binaries; without them the image ingestor catches the ``ImportError`` and
degrades instead of crashing, so shipping OCR is its own image decision.
"""


def _installed_extras() -> set[str]:
    """Return the ``--extra`` names the image's ``uv sync`` installs.

    Follows backslash continuations, so the flags may span several lines of
    the one ``RUN`` instruction exactly as Docker reads them.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    sync = re.search(
        r"uv sync --locked --no-dev --no-install-project"
        r"(?P<flags>(?:[^\n]*\\\n)*[^\n]*)",
        text,
    )
    assert sync is not None
    return set(re.findall(r"--extra (\S+)", sync["flags"]))


def test_image_installs_exactly_the_request_key_and_upload_parser_extras() -> None:
    """The image ships each advertised SDK and each upload route's parser, nothing more.

    A managed vault lists :class:`RequestKeyProvider` in
    ``request_key_providers`` (ADR-0015), so it must ship each vendor's SDK; and
    ``POST /v1/uploads`` routes every suffix in ``_EXTENSION_ROUTES`` to an
    ingestor that imports its parser from an extra. An image missing either
    accepts the request and then dies on ``ModuleNotFoundError`` (2026-10-23: a
    ``.docx`` upload, ``No module named 'docx'``). Exact equality keeps the image
    minimal: no other extra rides along.
    """
    expected = {provider.value for provider in RequestKeyProvider}
    expected |= set(_ROUTE_EXTRAS.values())
    assert _installed_extras() == expected


def test_every_upload_route_is_accounted_for() -> None:
    """A new upload route cannot ship without deciding where its parser comes from."""
    routes = {route for route in _EXTENSION_ROUTES.values() if isinstance(route, str)}
    accounted = (
        set(_ROUTE_EXTRAS) | _ROUTES_WITHOUT_EXTRA | _ROUTES_DEGRADING_WITHOUT_EXTRA
    )
    assert routes <= accounted, sorted(routes - accounted)


def test_image_has_nonroot_runtime_and_no_implicit_volume() -> None:
    """Operators must mount storage explicitly and the API runs unprivileged."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER [1-9][0-9]*:[1-9][0-9]*$", text, re.M)
    assert "\nVOLUME " not in text
    assert 'ENTRYPOINT ["python", "-m", "creek_mcp.container_runtime"]' in text


def test_image_healthcheck_uses_the_secret_reading_helper() -> None:
    """The image embeds a readiness probe without a bearer argument."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert 'CMD ["python", "-m", "creek_mcp.container_health"]' in text
    assert _has_no_secret_build_inputs(text)


def _has_no_secret_build_inputs(text: str) -> bool:
    """Return whether no Docker build instruction accepts a credential."""
    instructions = re.findall(r"^(?:ARG|ENV)\s+(.+)$", text, re.M)
    return all(
        word not in instruction.lower()
        for instruction in instructions
        for word in ("token", "secret", "password", "private_key")
    )


def test_build_context_excludes_repository_and_operator_state() -> None:
    """The build context cannot accidentally layer Git or local credentials."""
    ignored = set(DOCKERIGNORE.read_text(encoding="utf-8").splitlines())
    assert {".git", "**/.git", "**/.env", "**/.secrets*", "**/__pycache__"} <= ignored


def test_ci_builds_and_runs_the_real_container_contract() -> None:
    """The blocking CI gate builds the image and runs its Docker fixture."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    job = jobs["container-runtime"]
    steps = "\n".join(str(step) for step in job["steps"])
    assert "docker build" in steps
    assert "container-contract.sh" in steps
    assert "container-runtime" in jobs["quality-gate"]["needs"]


def test_contract_fixture_and_operator_runbook_are_committed() -> None:
    """The executable fixture and mount/restore guidance ship together."""
    assert CONTRACT_SCRIPT.is_file()
    assert DOC.is_file()
    doc = DOC.read_text(encoding="utf-8")
    for phrase in (
        "--read-only",
        "/vault",
        "/run/secrets",
        "recovery key",
        "image digest",
        "first boot",
    ):
        assert phrase in doc


def test_contract_uses_managed_volumes_and_linux_traversable_secret_root() -> None:
    """CI must exercise the image UID without depending on macOS bind semantics."""
    script = CONTRACT_SCRIPT.read_text(encoding="utf-8")
    assert 'chmod 0755 "$TMP_ROOT"' in script
    assert "docker volume create" in script
    assert "type=volume,src=$volume,dst=/vault" in script
    assert "docker volume rm --force" in script


def test_runbook_and_contract_grant_only_the_image_uid_secret_access() -> None:
    """The documented golden path must work without world-readable secrets."""
    doc = DOC.read_text(encoding="utf-8")
    script = CONTRACT_SCRIPT.read_text(encoding="utf-8")

    assert "chown -R 10001:10001 ./run-secrets" in doc
    assert "chmod 0511 ./run-secrets" in doc
    assert "chmod 0400" in doc
    assert "--user 0:0" in script
    assert "chown -R 10001:10001 /run/secrets" in script
    assert "chmod 0400" in script
    assert 'chmod 0444 "$root/secrets"/*' not in script


_BLOCK_NUMPY = """
import importlib.abc
import sys

class BlockNumpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "numpy" or fullname.startswith("numpy."):
            raise ModuleNotFoundError("optional numpy import attempted")
        return None

sys.meta_path.insert(0, BlockNumpy())
"""


def _run_without_numpy(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a fresh interpreter where NumPy is unavailable, as in the image."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_NUMPY + body],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_http_api_cold_import_does_not_require_the_optional_author_stack() -> None:
    """The base API image must start without importing optional NumPy code."""
    result = _run_without_numpy(
        'import creek_mcp.httpapi.app\nassert "creek.author" not in sys.modules\n'
    )
    assert result.returncode == 0, result.stderr


def test_the_combined_app_builds_its_mcp_surface_without_numpy(tmp_path: Path) -> None:
    """``create_app`` builds the ``/mcp`` server in the image's NumPy-free stack.

    Building the MCP server imports the whole tool registry (#1962); a
    module-level NumPy import anywhere on it would stop every managed vault at
    boot rather than at the first call that needed it.

    Args:
        tmp_path: A scratch vault directory.
    """
    result = _run_without_numpy(
        "from pathlib import Path\n"
        "from creek_mcp.httpapi.app import create_app\n"
        "from creek_mcp.remote_auth import ConsumerTokenVerifier\n"
        f"app = create_app(vault_path=Path({str(tmp_path)!r}), "
        "verifier=ConsumerTokenVerifier({'adepthood': ('t' * 32,)}))\n"
        "assert app.state.mcp_server is not None\n"
    )
    assert result.returncode == 0, result.stderr


def test_the_container_contract_completes_an_mcp_handshake_over_tls() -> None:
    """The real-Docker contract drives ``/mcp`` with the mounted consumer token.

    ``initialize``, then ``tools/call creek.handshake``, over the container's
    TLS listener, asserting the network transport — the in-process twin is
    ``tests/test_container_runtime.py``'s served-app handshake.
    """
    script = CONTRACT_SCRIPT.read_text(encoding="utf-8")
    assert "https://127.0.0.1:${port}/mcp" in script
    assert '"method":"initialize"' in script
    assert '"name":"creek.handshake"' in script
    assert 'handshake.get("transport") == "network"' in script
    assert 'mcp_handshake "adepthood-a" "$TOKEN_A" 18823' in script
    assert "mcp_handshake" in script


def test_reflection_route_imports_the_author_stack_without_numpy() -> None:
    """``POST /v1/reflections`` loads ``creek.author`` on first use; NumPy is an extra.

    The image installs no extras, so a module-level NumPy import anywhere on that
    lazy path fails the first reflection a vault ever serves (#1870).
    """
    result = _run_without_numpy(
        "import creek_mcp.httpapi.reflect\n"
        "import creek_mcp.tools.reflect\n"
        "import creek_mcp.tools.author\n"
        "from creek.author.agents import RetrievalSpecialist\n"
    )
    assert result.returncode == 0, result.stderr
