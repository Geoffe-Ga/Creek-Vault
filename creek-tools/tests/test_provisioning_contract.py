"""Versioned provisioning schema, auth, and runnable-service contract (#1768)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from creek_mcp.provisioning.api import CONTRACT_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = REPO_ROOT / "creek-tools"
OPENAPI = PROJECT_ROOT / "docs" / "contracts" / "provisioning-v1" / "openapi.json"
AUTH_DOC = PROJECT_ROOT / "docs" / "contracts" / "provisioning-v1" / "authentication.md"
RUNBOOK = PROJECT_ROOT / "docs" / "provisioning-control-plane.md"
CLI = PROJECT_ROOT / "creek_mcp" / "provisioning" / "cli.py"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def test_openapi_contract_is_versioned_and_matches_the_served_paths() -> None:
    """The checked-in language-neutral schema names this exact API minor."""
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))

    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["version"] == CONTRACT_VERSION
    assert set(contract["paths"]) == {
        "/control/v1/activations",
        "/control/v1/jobs/{job_id}",
        "/control/v1/jobs/{job_id}/retry",
    }
    assert contract["security"] == [{"consumerBearer": []}]


def test_public_schema_contains_no_credential_or_provider_result_field() -> None:
    """A browser-visible response cannot receive internal handoff material."""
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    serialized = json.dumps(schemas, sort_keys=True).lower()

    for forbidden in (
        "consumer_credential",
        "provider_token",
        "recovery_key",
        "volume_master_key",
        "vault_url",
        "passphrase",
    ):
        assert forbidden not in serialized


def test_authentication_contract_is_backend_only_and_file_mounted() -> None:
    """The auth document forbids browser tokens and bearer secrets in env/args."""
    text = " ".join(AUTH_DOC.read_text(encoding="utf-8").lower().split())

    for phrase in (
        "backend-to-backend",
        "never returned to browser",
        "mounted file",
        "one-time handoff",
        "tls",
    ):
        assert phrase in text


def test_runbook_states_the_fake_driver_and_key_ceremony_boundaries() -> None:
    """Operators cannot mistake this issue for Fly or no-escrow delivery."""
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "FakeProviderDriver" in text
    assert "#1770" in text
    assert "#1771" in text
    assert "provider work never runs in the API process" in text


def test_cli_is_installed_and_accepts_only_secret_file_paths() -> None:
    """The runnable API receives auth material through a mounted file."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    script = CLI.read_text(encoding="utf-8")

    assert (
        project["project"]["scripts"]["creek-provisioning-api"]
        == "creek_mcp.provisioning.cli:main"
    )
    assert "--consumer-tokens-file" in script
    assert re.search(r'"--consumer-token"', script) is None
    assert "CREEK_MCP_CONSUMER_TOKENS" not in script
    assert "require_transport_confidentiality" in script
