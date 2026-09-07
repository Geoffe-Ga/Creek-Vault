"""Static image and CI promises for the one-vault runtime (#1772)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

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


def test_http_api_cold_import_does_not_require_the_optional_author_stack() -> None:
    """The base API image must start without importing optional NumPy code."""
    code = """
import importlib.abc
import sys

class BlockNumpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "numpy" or fullname.startswith("numpy."):
            raise ModuleNotFoundError("optional numpy import attempted")
        return None

sys.meta_path.insert(0, BlockNumpy())
import creek_mcp.httpapi.app
assert "creek.author" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
