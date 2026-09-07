"""Versioned provisioning schema, auth, and runnable-service contract (#1768)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from creek_mcp.provisioning.api import CONTRACT_VERSION
from creek_mcp.provisioning.ceremony import (
    KEY_CEREMONY_VERSION,
    CeremonySubmission,
)
from creek_mcp.provisioning.store import MAX_ACTIVATION_ALIASES_PER_CONSUMER

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = REPO_ROOT / "creek-tools"
OPENAPI = PROJECT_ROOT / "docs" / "contracts" / "provisioning-v1" / "openapi.json"
AUTH_DOC = PROJECT_ROOT / "docs" / "contracts" / "provisioning-v1" / "authentication.md"
RUNBOOK = PROJECT_ROOT / "docs" / "provisioning-control-plane.md"
CEREMONY = PROJECT_ROOT / "docs" / "contracts" / "provisioning-v1" / "key-ceremony.md"
CEREMONY_VECTORS = (
    PROJECT_ROOT
    / "docs"
    / "contracts"
    / "provisioning-v1"
    / "key-ceremony-test-vectors.json"
)
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
        "/control/v1/jobs/{job_id}/key-ceremony",
        "/control/v1/jobs/{job_id}/retry",
    }
    assert contract["security"] == [{"consumerBearer": []}]


def test_public_schema_contains_no_credential_or_provider_result_field() -> None:
    """A browser-visible response cannot receive internal handoff material."""
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]

    def field_names(value: object) -> set[str]:
        """Return every exact object key from the language-neutral schema."""
        if isinstance(value, dict):
            return set(value) | {
                name for item in value.values() for name in field_names(item)
            }
        if isinstance(value, list):
            return {name for item in value for name in field_names(item)}
        return set()

    names = field_names(schemas)

    for forbidden in (
        "consumer_credential",
        "provider_token",
        "recovery_key",
        "volume_master_key",
        "vault_url",
        "passphrase",
    ):
        assert forbidden not in names


def test_key_ceremony_contract_is_versioned_strict_and_language_neutral() -> None:
    """The checked-in protocol and vector pin every cross-language primitive."""
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    submission = contract["components"]["schemas"]["CeremonySubmission"]
    vector = json.loads(CEREMONY_VECTORS.read_text(encoding="utf-8"))
    prose = " ".join(CEREMONY.read_text(encoding="utf-8").split())

    assert submission["additionalProperties"] is False
    assert submission["properties"]["protocol_version"]["const"] == KEY_CEREMONY_VERSION
    assert (
        CeremonySubmission.model_validate(vector["submission"]).protocol_version
        == KEY_CEREMONY_VERSION
    )
    for phrase in (
        "Argon2id",
        "HKDF-SHA256",
        "AES-256-GCM",
        "unrecoverable data loss",
        "shown or downloaded by the client exactly once",
        "attestation fails",
        "reconciles provider resources to zero",
    ):
        assert phrase in prose


def test_public_contract_publishes_the_durable_activation_alias_limit() -> None:
    """Clients can predict the bounded idempotency ledger's refusal boundary."""
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    activation = contract["components"]["schemas"]["ActivationRequest"]["properties"][
        "activation_id"
    ]
    runbook = RUNBOOK.read_text(encoding="utf-8")
    limit = str(MAX_ACTIVATION_ALIASES_PER_CONSUMER)

    assert limit in activation["description"]
    assert limit in runbook


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
