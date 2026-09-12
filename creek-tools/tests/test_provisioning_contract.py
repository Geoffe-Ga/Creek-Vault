"""Versioned provisioning schema, auth, and runnable-service contract (#1768)."""

from __future__ import annotations

import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from starlette.routing import Route

from creek_mcp.httpapi.provisioning import _job_payload, build_provisioning_app
from creek_mcp.provisioning.api import CONTRACT_VERSION
from creek_mcp.provisioning.ceremony import (
    KEY_CEREMONY_VERSION,
    CeremonySubmission,
)
from creek_mcp.provisioning.models import (
    FailureReason,
    JobOperation,
    JobState,
    ProvisioningJob,
)
from creek_mcp.provisioning.store import (
    MAX_ACTIVATION_ALIASES_PER_CONSUMER,
    ProvisioningStore,
)
from creek_mcp.remote_auth import ConsumerTokenVerifier

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
FLEET_CLI = PROJECT_ROOT / "creek_mcp" / "provisioning" / "fleet_cli.py"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
DOCS_INDEX = PROJECT_ROOT / "docs" / "README.md"
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
_JOB_WIRE_FIELDS = {
    "job_id",
    "activation_id",
    "state",
    "attempts",
    "retryable",
    "failure_reason",
    "created_at",
    "updated_at",
    "attested_confidential",
    "status_url",
}


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
        "requester identity",
        "requester's namespace",
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


def test_job_wire_and_enums_are_unchanged_by_fleet_work(tmp_path: Path) -> None:
    """Receipts, telemetry and alerts never reach the consumer-facing contract."""
    now = datetime(2026, 9, 12, tzinfo=UTC)
    job = ProvisioningJob(
        job_id="job-wire",
        activation_id="activation-wire",
        requester_identity="adepthood",
        consumer_identity="user-wire",
        state=JobState.READY,
        operation=JobOperation.CREATE,
        attempts=1,
        retryable=False,
        failure_reason=None,
        created_at=now,
        updated_at=now,
        attested_confidential=False,
    )
    contract = json.loads(OPENAPI.read_text(encoding="utf-8"))
    job_schema = contract["components"]["schemas"]["Job"]
    app = build_provisioning_app(
        ProvisioningStore(tmp_path / "wire.sqlite3"),
        ConsumerTokenVerifier({"adepthood": ("wire-test-token-" + "w" * 32,)}),
    )

    served = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, Route)
        for method in (route.methods or ())
        if method != "HEAD"
    }

    assert set(_job_payload(job)) == _JOB_WIRE_FIELDS
    assert set(job_schema["properties"]) == _JOB_WIRE_FIELDS
    assert job_schema["additionalProperties"] is False
    assert {state.value for state in JobState} == {
        "pending",
        "provisioning",
        "awaiting_key_ceremony",
        "ready",
        "failed",
        "deleting",
        "deleted",
    }
    assert {reason.value for reason in FailureReason} == {
        "provider_unavailable",
        "provider_rejected",
        "handoff_failed",
        "internal_error",
    }
    assert CONTRACT_VERSION == "1.1.0"
    assert served == {
        ("/control/v1/activations", "POST"),
        ("/control/v1/jobs/{job_id}", "GET"),
        ("/control/v1/jobs/{job_id}", "DELETE"),
        ("/control/v1/jobs/{job_id}/retry", "POST"),
        ("/control/v1/jobs/{job_id}/key-ceremony", "GET"),
        ("/control/v1/jobs/{job_id}/key-ceremony", "PUT"),
    }


def test_fleet_cli_is_installed_and_accepts_only_secret_file_paths() -> None:
    """The fleet tool is an entry point whose provider token arrives as a file."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    script = FLEET_CLI.read_text(encoding="utf-8")

    assert (
        project["project"]["scripts"]["creek-provisioning-fleet"]
        == "creek_mcp.provisioning.fleet_cli:main"
    )
    for argument in (
        "--database",
        "--policy-file",
        "--fly-token-file",
        "--fly-organization",
        "--fly-image",
        "--fly-api-base-url",
        "--fly-token-expires-at",
        "--inventory-file",
        "--record-month",
        "--confidential-compute-changed",
    ):
        assert argument in script
    assert re.search(r'"--fly-token"', script) is None
    assert "RefusingSecretManager" in script
    for subcommand in ("report", "reconcile", "emergency-stop"):
        assert f'"{subcommand}"' in script


def test_runbook_documents_fleet_reconciliation_invoice_and_review_checkpoint() -> None:
    """Operators receive the alerts, invoice, emergency-stop and D7 procedures."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    text = " ".join(runbook.lower().split())
    index_row = next(
        line
        for line in DOCS_INDEX.read_text(encoding="utf-8").splitlines()
        if "provisioning-control-plane.md" in line
    )
    changelog = CHANGELOG.read_text(encoding="utf-8")
    unreleased = changelog.split("## Unreleased", 1)[1]

    for heading in (
        "## Fleet reconciliation, telemetry, and budget alarms (#1769)",
        "### Alerts",
        "### Invoice reconciliation",
        "### Emergency fleet stop",
        "### Review checkpoint (ADR-0013 Decision 7)",
    ):
        assert heading in runbook
    for phrase in (
        "invoice reconciliation",
        "emergency fleet stop",
        "destroys no volume",
        "without detaching its volume",
        "injected assumptions",
        "never as business logic",
        "--inventory-file",
        "no org-wide listing",
        "500 activated vaults",
        "1,000 provisioned volumes",
        "confidential-compute availability",
        "three rolling months",
        "interrupts background work",
        "reference assumptions from adr-0013 decision 4, not defaults",
        "equal fires",
        "only when the allocation is live",
        "report --record-month yyyy-mm",
        "refuses a month that has not ended",
        "calendar-consecutive recorded months",
    ):
        assert phrase in text
    for field in (
        "provisioned_volumes",
        "stopped_rootfs_gb",
        "running_machine_seconds_fleet",
        "running_seconds_injected",
        "snapshot_bytes",
        "egress_bytes",
        "duplicate_allocation_attempts",
        "orphan_resources",
    ):
        assert field in runbook
    assert "fleet reconciliation" in index_row
    assert "budget alarms" in index_row
    assert "#1769" in unreleased
    assert "ADR-0013 Decisions 4, 6, and 7" in unreleased
