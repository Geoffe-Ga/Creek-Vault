"""Sanitized, offline evidence contract for the managed Fly pilot (#1806)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from creek_mcp.provisioning.pilot_evidence import (
    ARTIFACT_KINDS,
    CostFacts,
    PilotEvidenceInput,
    PilotPrerequisiteDocument,
    reduce_pilot_evidence,
)
from creek_mcp.provisioning.pilot_evidence_cli import main

_NOW = "2026-09-14T12:00:00Z"
_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_PRIVATE_CANARIES = (
    "app-private-123",
    "machine-private-456",
    "volume-private-789",
    "job-private-abc",
    "https://private-vault.example.test",
    "owner@example.test",
    "secret-token-canary",
)


def _artifact_files(tmp_path: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for index, kind in enumerate(sorted(ARTIFACT_KINDS)):
        path = tmp_path / f"private-{index}.json"
        path.write_text(
            json.dumps({"kind": kind, "private": _PRIVATE_CANARIES[index % 7]}),
            encoding="utf-8",
        )
        artifacts.append({"kind": kind, "observed_at": _NOW, "path": str(path)})
    return artifacts


def _common_drill(**extra: object) -> dict[str, object]:
    return {
        "outcome": "passed",
        "reversible": True,
        "destructive_provider_mutations": 0,
        "unexpected_billable_resources": 0,
        **extra,
    }


def _document(tmp_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "executed_at": _NOW,
        "source": {
            "creek_sha": "c" * 40,
            "control_image_digest": _DIGEST,
            "vault_image_digest": _OTHER_DIGEST,
            "provisioning_contract": "2.1.0",
            "vault_contract": "0.16.0",
        },
        "authorization": {
            "dedicated_organization": True,
            "max_live_allocations": 5,
            "currency": "USD",
            "monthly_alert_usd_cents": 2500,
            "region_recorded": True,
            "cleanup_owner_recorded": True,
        },
        "credentials": {
            "deploy_token_scope": "organization_deploy",
            "evidence_token_scope": "organization_read_only",
            "deploy_token_lifetime_seconds": 604800,
            "evidence_token_lifetime_seconds": 3600,
            "deploy_token_age_seconds": 86400,
            "evidence_token_age_seconds": 1800,
            "personal_token_values": 0,
            "secret_values_in_environment": 0,
            "mounted_secret_file_modes_0400": True,
            "deploy_token_revoked": True,
            "evidence_token_revoked": True,
        },
        "deployments": {
            "control_revision": "d" * 40,
            "worker_revision": "e" * 40,
            "routing_revision": "f" * 40,
            "fleet_schedule_revision": "1" * 40,
            "control_image_digest_matches_deployment": True,
            "vault_image_digest_matches_deployment": True,
            "shared_atomic_state_storage": True,
            "public_tls_healthy": True,
            "authenticated_health_checks_healthy": True,
            "worker_ready_signal_healthy": True,
        },
        "fault_drills": {
            "create_after_provider_create_before_handoff": _common_drill(
                retryable_job_persisted=True,
                restarted_worker_converged=True,
            ),
            "provider_outage": _common_drill(
                provider_unavailable_persisted=True,
                retry_converged=True,
            ),
            "callback_outage": _common_drill(
                handoff_failure_persisted=True,
                retry_converged=True,
            ),
            "readiness_timeout": _common_drill(
                route_refused=True,
                same_machine_after_restore=True,
            ),
            "routing_failure": _common_drill(
                unavailable_response_observed=True,
                same_endpoint_after_restore=True,
            ),
        },
        "private_cardinality": {
            "after_create": {
                "apps": ["app-private-123"],
                "machines": ["machine-private-456"],
                "volumes": ["volume-private-789"],
                "credentials": ["credential-private-321"],
                "jobs": ["job-private-abc"],
            },
            "after_teardown": {
                "apps": [],
                "machines": [],
                "volumes": [],
                "credentials": [],
                "jobs": [],
            },
            "teardown_receipt_confirmed": True,
            "teardown_idempotent": True,
        },
        "fleet": {
            "provider_authoritative_inventory": True,
            "inventory_mode": "derived",
            "alert_test_report_exit_code": 3,
            "alert_kind": "monthly_budget_departure",
            "alert_delivered": True,
            "alert_acknowledged": True,
            "final_report_exit_code": 0,
            "final_alert_count": 0,
            "final_divergence_count": 0,
            "reconcile_window_approved": True,
            "reconcile_interruption_window_seconds": 900,
            "retryable_deletions_requeued": 1,
            "nonretryable_deletions_requeued": 0,
        },
        "restores": {
            "sqlite_runtime_state": {
                "outcome": "passed",
                "network_mutations_disabled": True,
                "schema_valid": True,
                "job_membership_equal": True,
                "allocation_membership_equal": True,
                "credential_membership_equal": True,
                "receipt_membership_equal": True,
                "idempotent_restart": True,
            },
            "stopped_volume": {
                "outcome": "passed",
                "source_machine_stopped": True,
                "snapshot_created": True,
                "same_region": True,
                "reviewed_image_digest_matched": True,
                "sentinel_equal": True,
                "original_volume_preserved": True,
                "temporary_machine_removed": True,
                "temporary_volume_removed": True,
            },
        },
        "emergency_stop": {
            "outcome": "passed",
            "non_destructive": True,
            "stopped_count": 1,
            "failed_count": 0,
            "routing_recovered": True,
            "deletion_converged": True,
        },
        "cost": {
            "currency": "USD",
            "estimated_usd": "1.23",
            "provider_total_usd": "1.25",
            "signed_delta_usd": "0.02",
            "rate_date": "2026-09-14",
            "rate_source": "provider_invoice",
            "unpriced_inputs": [],
            "unknown_inputs_treated_as_zero": False,
            "reconciled": True,
        },
        "exact_main": {
            "ci_green": True,
            "deployment_verified": True,
            "unexplained_product_skips": 0,
        },
        "independent_review": {
            "reviewed_creek_sha": "c" * 40,
            "verdict": "LGTM",
            "reference_kind": "github_issuecomment",
            "reference_number": 123456,
            "artifact_kind": "independent_review",
        },
        "private_artifacts": _artifact_files(tmp_path),
        "private_context": {
            "organization_id": "org-private-000",
            "alert_subject": "owner@example.test",
            "vault_url": "https://private-vault.example.test",
        },
    }


def test_reducer_emits_closed_content_free_passed_prerequisite(tmp_path: Path) -> None:
    """The reducer derives counts and hashes without leaking private values."""
    document = _document(tmp_path)
    document["private_artifacts"].reverse()
    evidence = reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))
    rendered = evidence.model_dump_json()

    assert evidence.status == "passed"
    assert evidence.cardinality.after_create.model_dump() == {
        "app_count": 1,
        "machine_count": 1,
        "volume_count": 1,
        "credential_count": 1,
        "job_count": 1,
    }
    assert evidence.cardinality.after_teardown.model_dump() == {
        "app_count": 0,
        "machine_count": 0,
        "volume_count": 0,
        "credential_count": 0,
        "job_count": 0,
    }
    assert {item.kind for item in evidence.artifacts} == ARTIFACT_KINDS
    assert [item.kind for item in evidence.artifacts] == sorted(ARTIFACT_KINDS)
    assert all(item.sha256.startswith("sha256:") for item in evidence.artifacts)
    assert evidence.independent_review.artifact_sha256 == next(
        item.sha256 for item in evidence.artifacts if item.kind == "independent_review"
    )
    assert evidence.model_dump()["schema_version"] == "1.0.0"
    assert evidence.model_dump_json(exclude_none=False).count("http") == 0
    for canary in _PRIVATE_CANARIES:
        assert canary not in rendered


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("authorization", "monthly_alert_usd_cents"), 2499),
        (("credentials", "personal_token_values"), 1),
        (("credentials", "secret_values_in_environment"), 1),
        (("credentials", "deploy_token_age_seconds"), 604801),
        (("fault_drills", "provider_outage", "unexpected_billable_resources"), 1),
        (("fleet", "final_report_exit_code"), 3),
        (("fleet", "final_alert_count"), 1),
        (("cost", "unknown_inputs_treated_as_zero"), True),
        (("cost", "unpriced_inputs"), ["egress"]),
        (("cost", "estimated_usd"), "-1.00"),
        (("cost", "provider_total_usd"), "-1.00"),
        (("cost", "rate_date"), "2026-02-30"),
        (("executed_at",), "2026-02-30T12:00:00Z"),
        (("independent_review", "reviewed_creek_sha"), "0" * 40),
    ],
)
def test_reducer_rejects_false_positive_evidence(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    """A passed record cannot be produced when a mandatory proof is false."""
    document = _document(tmp_path)
    target: dict[str, Any] = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises((ValidationError, ValueError)):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))


def test_input_is_closed_and_artifact_set_is_exact(tmp_path: Path) -> None:
    """Unknown claims and missing or duplicate artifacts cannot be smuggled in."""
    document = _document(tmp_path)
    document["invented_success"] = True
    with pytest.raises(ValidationError):
        PilotEvidenceInput.model_validate(document)

    document = _document(tmp_path)
    document["private_artifacts"].pop()
    with pytest.raises(ValueError, match="artifact kind set"):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))

    document = _document(tmp_path)
    document["private_artifacts"][1]["kind"] = document["private_artifacts"][0]["kind"]
    with pytest.raises(ValueError, match="artifact kind set"):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))


def test_artifact_paths_must_be_files_and_timestamps_use_utc_seconds(
    tmp_path: Path,
) -> None:
    """Private artifact hashing is local-only and timestamp syntax is stable."""
    document = _document(tmp_path)
    document["private_artifacts"][0]["path"] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="private artifact is unreadable"):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))

    document = _document(tmp_path)
    document["executed_at"] = datetime(2026, 9, 14, 12, tzinfo=UTC).isoformat()
    with pytest.raises(ValidationError):
        PilotEvidenceInput.model_validate(document)


def test_cost_totals_are_unsigned_even_when_signed_delta_reconciles() -> None:
    """A negative estimate/provider total cannot masquerade as reconciled cost."""
    with pytest.raises(ValidationError):
        CostFacts.model_validate(
            {
                "currency": "USD",
                "estimated_usd": "-1.00",
                "provider_total_usd": "-0.98",
                "signed_delta_usd": "0.02",
                "rate_date": "2026-09-14",
                "rate_source": "provider_invoice",
                "unpriced_inputs": [],
                "unknown_inputs_treated_as_zero": False,
                "reconciled": True,
            }
        )


def test_private_cardinality_is_allocation_scoped_and_duplicate_free(
    tmp_path: Path,
) -> None:
    """Counts are derived from unique IDs rather than operator-entered totals."""
    document = _document(tmp_path)
    document["private_cardinality"]["after_create"]["machines"].append(
        "machine-private-456"
    )
    with pytest.raises(ValueError, match="unique"):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))

    document = _document(tmp_path)
    document["private_cardinality"]["after_create"]["volumes"] = []
    with pytest.raises(ValueError, match="after-create cardinality"):
        reduce_pilot_evidence(PilotEvidenceInput.model_validate(document))


def test_cli_emits_named_prerequisite_block_without_private_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI emits the exact Adepthood-consumable wrapper on stdout."""
    input_file = tmp_path / "private-input.json"
    input_file.write_text(json.dumps(_document(tmp_path)), encoding="utf-8")

    assert main(["reduce", "--input", str(input_file)]) == 0

    output = capsys.readouterr()
    parsed = PilotPrerequisiteDocument.model_validate_json(output.out)
    assert parsed.managed_vault_pilot_prerequisite.status == "passed"
    assert output.err == ""
    for canary in _PRIVATE_CANARIES:
        assert canary not in output.out


def test_cli_failure_is_generic_and_does_not_echo_private_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parser and validation failures never copy sensitive values to stderr."""
    input_file = tmp_path / "private-input.json"
    document = _document(tmp_path)
    document["source"]["creek_sha"] = "secret-token-canary"
    input_file.write_text(json.dumps(document), encoding="utf-8")

    assert main(["reduce", "--input", str(input_file)]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "pilot evidence input is invalid\n"


def test_cli_schema_is_closed_versioned_and_names_every_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The checked contract can be consumed without importing Python models."""
    assert main(["schema"]) == 0

    schema = json.loads(capsys.readouterr().out)
    root = schema["properties"]["managed_vault_pilot_prerequisite"]
    assert root["$ref"] == "#/$defs/PilotPrerequisite"
    prerequisite = schema["$defs"]["PilotPrerequisite"]
    assert prerequisite["additionalProperties"] is False
    assert prerequisite["properties"]["schema_version"]["const"] == "1.0.0"
    artifact = schema["$defs"]["ArtifactFact"]
    assert set(artifact["properties"]["kind"]["enum"]) == ARTIFACT_KINDS


def test_runbook_has_reviewed_bounded_reversible_fault_procedures() -> None:
    """Every required fault has a safe procedure and ordinary-prod guard."""
    runbook = Path("docs/managed-vault-fly-pilot.md").read_text(encoding="utf-8")
    for heading in (
        "Create after provider create, before handoff",
        "Provider outage",
        "Callback outage",
        "Readiness timeout",
        "Routing failure",
    ):
        assert f"### {heading}" in runbook
    assert "Production executables accept no fault-injection flag" in runbook
    assert "Never revoke a token to create a fault" in runbook
    assert "one allocation" in runbook
    assert "abort the drill" in runbook
