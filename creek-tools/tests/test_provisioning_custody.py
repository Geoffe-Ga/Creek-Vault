"""Honest ordinary-Fly custody contract for managed vaults (#1808)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from creek_mcp.httpapi.provisioning import build_provisioning_app
from creek_mcp.provisioning.driver import FakeOneTimeHandoff, FakeProviderDriver
from creek_mcp.provisioning.models import CustodyMode, JobState
from creek_mcp.provisioning.store import _SCHEMA, ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from creek_mcp.remote_auth import ConsumerTokenVerifier

_NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)
_TOKEN = "custody-test-consumer-token-" + "c" * 32
_OPENAPI = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "provisioning-v1"
    / "openapi.json"
)
_ADR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "architecture"
    / "ADR"
    / "0014-provider-managed-custody-for-ordinary-fly.md"
)
_RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "provisioning-control-plane.md"
)
_ARCHIVED_CEREMONY = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "provisioning-v1"
    / "key-ceremony.md"
)


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return one real durable store for custody transition tests."""
    return ProvisioningStore(tmp_path / "custody.sqlite3")


def test_ordinary_worker_handoffs_directly_to_provider_managed_ready(
    store: ProvisioningStore,
) -> None:
    """Ordinary Fly never creates or waits on a non-operative key ceremony."""
    submitted = store.submit("activation-custody", "user-custody", now=_NOW)
    handoff = FakeOneTimeHandoff()

    assert ProvisioningWorker(store, FakeProviderDriver(), handoff).run_once(now=_NOW)
    completed = store.get(submitted.job_id, "user-custody")

    assert completed is not None
    assert completed.state is JobState.READY
    assert completed.custody_mode is CustodyMode.PROVIDER_MANAGED
    assert completed.attested_confidential is False
    assert handoff.delivery_count == 1
    assert store.get_wrapped_key_artifact(submitted.job_id, "user-custody") is None


def test_public_contract_names_provider_custody_and_exposes_no_ceremony_route(
    store: ProvisioningStore,
) -> None:
    """Wire consumers cannot mistake ordinary provider encryption for no escrow."""
    verifier = ConsumerTokenVerifier({"adepthood": (_TOKEN,)})
    app = build_provisioning_app(store, verifier)
    client = TestClient(app)
    created = client.post(
        "/control/v1/activations",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={"activation_id": "activation-wire", "consumer_identity": "user-wire"},
    )
    job_id = created.json()["job_id"]
    assert ProvisioningWorker(
        store,
        FakeProviderDriver(),
        FakeOneTimeHandoff(),
    ).run_once(now=_NOW)

    status = client.get(
        f"/control/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    served_paths = {route.path for route in app.routes if isinstance(route, Route)}
    contract = json.loads(_OPENAPI.read_text(encoding="utf-8"))

    assert status.status_code == 200
    assert status.json()["custody_mode"] == "provider_managed"
    assert status.json()["attested_confidential"] is False
    assert "/control/v1/jobs/{job_id}/key-ceremony" not in served_paths
    assert "/control/v1/jobs/{job_id}/key-ceremony" not in contract["paths"]
    assert contract["components"]["schemas"]["Job"]["properties"]["custody_mode"][
        "enum"
    ] == ["provider_managed", "wrapped_artifact_only", None]


def test_v4_rows_migrate_to_truthful_custody_modes(tmp_path: Path) -> None:
    """Existing ceremony receipts never become provider custody claims."""
    database = tmp_path / "provisioning-v4.sqlite3"
    old_schema = _SCHEMA.replace("    custody_mode TEXT,\n", "").replace(
        ",\n    CHECK (custody_mode IS NULL OR custody_mode IN (\n"
        "        'provider_managed', 'wrapped_artifact_only'\n"
        "    ))",
        "",
    )
    stamp = _NOW.isoformat(timespec="microseconds")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(old_schema)
        for job_id, state in (
            ("legacy-direct-ready", "ready"),
            ("legacy-ceremony", "awaiting_key_ceremony"),
            ("legacy-completed-ceremony", "ready"),
        ):
            connection.execute(
                "INSERT INTO provisioning_jobs "
                "(job_id, canonical_activation_id, requester_identity, "
                "consumer_identity, state, operation, attested_confidential, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'create', ?, ?, ?)",
                (
                    job_id,
                    f"activation-{job_id}",
                    "adepthood",
                    job_id,
                    state,
                    int(job_id == "legacy-completed-ceremony"),
                    stamp,
                    stamp,
                ),
            )
        connection.executemany(
            "INSERT INTO provisioning_key_ceremonies "
            "(job_id, ceremony_id, server_nonce, expires_at) VALUES (?, ?, ?, ?)",
            (
                ("legacy-ceremony", "ceremony-legacy", "nonce-a", stamp),
                (
                    "legacy-completed-ceremony",
                    "ceremony-completed",
                    "nonce-b",
                    stamp,
                ),
            ),
        )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

    migrated = ProvisioningStore(database)
    provider_managed = migrated.get("legacy-direct-ready", "adepthood")
    artifact_only = migrated.get("legacy-ceremony", "adepthood")
    completed_artifact_only = migrated.get(
        "legacy-completed-ceremony",
        "adepthood",
    )

    assert provider_managed is not None
    assert provider_managed.custody_mode is CustodyMode.PROVIDER_MANAGED
    assert provider_managed.attested_confidential is False
    assert artifact_only is not None
    assert artifact_only.custody_mode is CustodyMode.WRAPPED_ARTIFACT_ONLY
    assert artifact_only.attested_confidential is False
    assert completed_artifact_only is not None
    assert completed_artifact_only.custody_mode is CustodyMode.WRAPPED_ARTIFACT_ONLY
    assert completed_artifact_only.attested_confidential is False
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_interrupted_v4_migration_finishes_custody_backfill_idempotently(
    tmp_path: Path,
) -> None:
    """A prior committed ALTER cannot make the semantic backfill disappear."""
    database = tmp_path / "interrupted-provisioning-v4.sqlite3"
    stamp = _NOW.isoformat(timespec="microseconds")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO provisioning_jobs "
            "(job_id, canonical_activation_id, requester_identity, "
            "consumer_identity, state, operation, attested_confidential, "
            "custody_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ready', 'create', 1, NULL, ?, ?)",
            (
                (
                    "interrupted-direct-ready",
                    "activation-interrupted-direct",
                    "adepthood",
                    "user-interrupted-direct",
                    stamp,
                    stamp,
                ),
                (
                    "interrupted-completed-ceremony",
                    "activation-interrupted-ceremony",
                    "adepthood",
                    "user-interrupted-ceremony",
                    stamp,
                    stamp,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO provisioning_key_ceremonies "
            "(job_id, ceremony_id, server_nonce, expires_at) VALUES (?, ?, ?, ?)",
            (
                "interrupted-completed-ceremony",
                "ceremony-interrupted",
                "nonce-interrupted",
                stamp,
            ),
        )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

    for _ in range(2):
        migrated_store = ProvisioningStore(database)
        provider_managed = migrated_store.get(
            "interrupted-direct-ready",
            "adepthood",
        )
        artifact_only = migrated_store.get(
            "interrupted-completed-ceremony",
            "adepthood",
        )
        assert provider_managed is not None
        assert provider_managed.custody_mode is CustodyMode.PROVIDER_MANAGED
        assert provider_managed.attested_confidential is False
        assert artifact_only is not None
        assert artifact_only.custody_mode is CustodyMode.WRAPPED_ARTIFACT_ONLY
        assert artifact_only.attested_confidential is False

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)


def test_durable_docs_name_actual_readability_restart_and_intimate_boundary() -> None:
    """Support copy cannot silently revive the false no-escrow promise."""
    text = " ".join((_ADR.read_text() + _RUNBOOK.read_text()).split())

    for phrase in (
        "custody_mode=provider_managed",
        "passphrase and recovery key protect **nothing**",
        "sufficiently privileged Creek operator can read mounted bytes",
        "Fly supplies the unlock capability",
        "backup/restore is the only MVP recovery mechanism",
        "INTIMATE remains local",
    ):
        assert phrase in text


def test_archived_ceremony_refutes_fly_data_loss_and_current_client_flow() -> None:
    """Historical factor loss is never represented as Fly volume loss."""
    text = " ".join(_ARCHIVED_CEREMONY.read_text().split())

    assert "Losing both the passphrase and recovery code is permanent" not in text
    assert "did **not** make an ordinary Fly vault unrecoverable" in text
    assert "Current clients must not perform this flow" in text
