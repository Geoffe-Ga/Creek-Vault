"""User-held activation key ceremony and attested release contract (#1771)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient

from creek.confidential.keyvault import (
    KeyVault,
    KeyVaultBinding,
    UnlockError,
    create_bound_key_vault,
    unlock_with_passphrase,
    unlock_with_recovery,
)
from creek_mcp.httpapi.provisioning import build_provisioning_app
from creek_mcp.provisioning.ceremony import (
    KEY_CEREMONY_VERSION,
    AttestationStatement,
    CeremonyConflictError,
    CeremonyExpiredError,
    CeremonySubmission,
    Ed25519AttestationVerifier,
    FakeKeyReleaseSink,
    KeyCeremonyChallenge,
    KeyCeremonyService,
    KeyReleaseEnvelope,
    attestation_signed_payload,
)
from creek_mcp.provisioning.driver import FakeOneTimeHandoff, FakeProviderDriver
from creek_mcp.provisioning.models import JobState
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from creek_mcp.remote_auth import ConsumerTokenVerifier

_NOW = datetime(2026, 9, 7, 8, tzinfo=UTC)
_PASSPHRASE = "correct horse test battery staple"
_RECOVERY = "AEAQC-AIBAE-AQCAI-BAEAQ-CAIBA-EAQCA-IBAEA-QCAIB-AEAQC-AIBAE-AQ"
_VMK = bytes(32)
_TOKEN = "ceremony-api-test-token-" + "a" * 32
_OTHER_TOKEN = "ceremony-api-other-token-" + "b" * 32
_VECTOR = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "provisioning-v1"
    / "key-ceremony-test-vectors.json"
)


def _awaiting_store(tmp_path: Path) -> tuple[ProvisioningStore, str]:
    """Return one real store whose provider create reached the ceremony boundary."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    job = store.submit("activation-ceremony", "adepthood", now=_NOW)
    worker = ProvisioningWorker(store, FakeProviderDriver(), FakeOneTimeHandoff())
    assert worker.run_once(now=_NOW) is True
    awaiting = store.get(job.job_id, "adepthood")
    assert awaiting is not None
    assert awaiting.state is JobState.AWAITING_KEY_CEREMONY
    return store, job.job_id


def _vector_submission() -> CeremonySubmission:
    """Return the language-neutral vector as one strict wire submission."""
    vector = json.loads(_VECTOR.read_text(encoding="utf-8"))
    return CeremonySubmission.model_validate(vector["submission"])


def _for_challenge(
    submission: CeremonySubmission,
    challenge: KeyCeremonyChallenge,
) -> CeremonySubmission:
    """Rebind vector ciphertext for server-state tests without production helpers."""
    raw = submission.model_dump(mode="json")
    raw["ceremony_id"] = challenge.ceremony_id
    raw["server_nonce"] = challenge.server_nonce
    raw_binding = raw["wrapped_artifact"]["binding"]
    raw_binding["activation_id"] = challenge.activation_id
    raw_binding["ceremony_id"] = challenge.ceremony_id
    raw_binding["server_nonce"] = challenge.server_nonce
    return CeremonySubmission.model_validate(raw)


def _with_attested_release(
    submission: CeremonySubmission,
    statement: AttestationStatement,
    envelope: KeyReleaseEnvelope,
) -> CeremonySubmission:
    """Return one revalidated test submission carrying the attested release."""
    raw = submission.model_dump(mode="json")
    raw["attestation"] = statement.model_dump(mode="json")
    raw["key_release"] = envelope.model_dump(mode="json")
    return CeremonySubmission.model_validate(raw)


def _b64(value: bytes) -> str:
    """Return unpadded URL-safe base64 used by the wire contract."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _attestation(
    challenge: KeyCeremonyChallenge,
    root: Ed25519PrivateKey,
    recipient: X25519PublicKey,
    *,
    issued_at: datetime,
    expires_at: datetime,
) -> AttestationStatement:
    """Sign one synthetic measured-recipient statement."""
    recipient_bytes = recipient.public_bytes(Encoding.Raw, PublicFormat.Raw)
    unsigned = AttestationStatement(
        format="creek-ed25519-x25519-v1",
        measurement="sha384:expected",
        challenge_nonce=challenge.server_nonce,
        recipient_public_key=_b64(recipient_bytes),
        issued_at=issued_at,
        expires_at=expires_at,
        signature=_b64(bytes(64)),
    )
    return unsigned.model_copy(
        update={"signature": _b64(root.sign(attestation_signed_payload(unsigned)))}
    )


def _release_envelope(recipient: X25519PublicKey) -> KeyReleaseEnvelope:
    """Return one structurally valid opaque synthetic key-release envelope."""
    ephemeral = X25519PrivateKey.generate().public_key()
    return KeyReleaseEnvelope(
        algorithm="x25519-hkdf-sha256-aes256gcm",
        recipient_public_key=_b64(
            recipient.public_bytes(Encoding.Raw, PublicFormat.Raw)
        ),
        ephemeral_public_key=_b64(
            ephemeral.public_bytes(Encoding.Raw, PublicFormat.Raw)
        ),
        nonce=_b64(bytes(12)),
        ciphertext=_b64(bytes(48)),
    )


def _api_client(store: ProvisioningStore) -> TestClient:
    """Return the authenticated public ceremony API over *store*."""
    verifier = ConsumerTokenVerifier(
        {"adepthood": (_TOKEN,), "other-consumer": (_OTHER_TOKEN,)}
    )
    return TestClient(build_provisioning_app(store, verifier))


def _headers(token: str = _TOKEN) -> dict[str, str]:
    """Return one accepted synthetic bearer header."""
    return {"Authorization": f"Bearer {token}"}


def test_language_neutral_vector_unwraps_identically_by_both_user_factors() -> None:
    """The published artifact recovers the same VMK through both independent paths."""
    vector = json.loads(_VECTOR.read_text(encoding="utf-8"))
    artifact = KeyVault.from_dict(vector["submission"]["wrapped_artifact"])

    assert (
        unlock_with_passphrase(artifact, vector["client_inputs"]["passphrase"]) == _VMK
    )
    assert (
        unlock_with_recovery(artifact, vector["client_inputs"]["recovery_code"]) == _VMK
    )
    assert artifact.binding == KeyVaultBinding.from_dict(vector["binding"])


def test_bound_artifact_cannot_be_replayed_under_another_activation() -> None:
    """Changing public binding data invalidates the AEAD tags on both unwrap paths."""
    vector = json.loads(_VECTOR.read_text(encoding="utf-8"))
    artifact = vector["submission"]["wrapped_artifact"]
    artifact["binding"]["activation_id"] = "another-activation"
    rebound = KeyVault.from_dict(artifact)

    with pytest.raises(UnlockError):
        unlock_with_passphrase(rebound, _PASSPHRASE)
    with pytest.raises(UnlockError):
        unlock_with_recovery(rebound, _RECOVERY)


def test_reference_client_creates_a_fresh_bound_ciphertext_only_artifact() -> None:
    """The Python reference generator authenticates the complete public binding."""
    binding = KeyVaultBinding(
        protocol_version=KEY_CEREMONY_VERSION,
        activation_id="activation-reference",
        ceremony_id="ceremony-reference",
        server_nonce="A" * 43,
        client_nonce="B" * 43,
    )

    setup = create_bound_key_vault(_PASSPHRASE, binding)
    recovered = unlock_with_passphrase(setup.vault, _PASSPHRASE)

    assert setup.vault.version == 2
    assert setup.vault.binding == binding
    assert unlock_with_recovery(setup.vault, setup.recovery_key) == recovered
    persisted = json.dumps(setup.vault.to_dict())
    assert _PASSPHRASE not in persisted
    assert setup.recovery_key not in persisted
    assert recovered.hex() not in persisted


def test_wire_submission_cannot_carry_user_secrets_or_skip_recovery_ack() -> None:
    """Passphrase, recovery material, and VMK are absent from the accepted schema."""
    raw = _vector_submission().model_dump(mode="json")
    raw["passphrase"] = _PASSPHRASE
    with pytest.raises(ValueError):
        CeremonySubmission.model_validate(raw)

    raw.pop("passphrase")
    for unconfirmed in (False, 1, "true"):
        raw["recovery_saved"] = unconfirmed
        with pytest.raises(ValueError):
            CeremonySubmission.model_validate(raw)


def test_wire_submission_requires_explicit_attestation_disposition() -> None:
    """Clients must explicitly choose ordinary nulls or an attested key release."""
    raw = _vector_submission().model_dump(mode="json")
    raw.pop("attestation")
    raw.pop("key_release")

    with pytest.raises(ValueError):
        CeremonySubmission.model_validate(raw)


@pytest.mark.parametrize("missing", ["attestation", "key_release"])
def test_attestation_and_key_release_are_an_atomic_pair(missing: str) -> None:
    """Neither half of an attested release is accepted on its own."""
    raw = _vector_submission().model_dump(mode="json")
    challenge = KeyCeremonyChallenge(
        job_id="job",
        activation_id=raw["wrapped_artifact"]["binding"]["activation_id"],
        ceremony_id=raw["ceremony_id"],
        server_nonce=raw["server_nonce"],
        expires_at=_NOW + timedelta(hours=1),
    )
    root = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate().public_key()
    attested = _with_attested_release(
        CeremonySubmission.model_validate(raw),
        _attestation(
            challenge,
            root,
            recipient,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
        ),
        _release_envelope(recipient),
    ).model_dump(mode="json")
    attested[missing] = None

    with pytest.raises(ValueError):
        CeremonySubmission.model_validate(attested)


@pytest.mark.parametrize("field", ["format", "algorithm"])
def test_attested_wire_submission_requires_explicit_algorithm_markers(
    field: str,
) -> None:
    """Version/algorithm markers cannot silently default across implementations."""
    raw = _vector_submission().model_dump(mode="json")
    challenge = KeyCeremonyChallenge(
        job_id="job",
        activation_id=raw["wrapped_artifact"]["binding"]["activation_id"],
        ceremony_id=raw["ceremony_id"],
        server_nonce=raw["server_nonce"],
        expires_at=_NOW + timedelta(hours=1),
    )
    root = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate().public_key()
    statement = _attestation(
        challenge,
        root,
        recipient,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    attested = _with_attested_release(
        CeremonySubmission.model_validate(raw),
        statement,
        _release_envelope(recipient),
    ).model_dump(mode="json")
    nested = "attestation" if field == "format" else "key_release"
    attested[nested].pop(field)

    with pytest.raises(ValueError):
        CeremonySubmission.model_validate(attested)


def test_ordinary_machine_completes_without_advertising_attested_confidentiality(
    tmp_path: Path,
) -> None:
    """No quote means no key release and an honest non-attested capability result."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    submission = _for_challenge(_vector_submission(), challenge)
    service = KeyCeremonyService(store)

    completed = service.complete(job_id, "adepthood", submission, now=_NOW)
    repeated = service.complete(job_id, "adepthood", submission, now=_NOW)

    assert completed == repeated
    assert completed.state is JobState.READY
    assert completed.attested_confidential is False
    assert store.get_wrapped_key_artifact(job_id, "adepthood") is not None
    assert challenge.expires_at == _NOW + timedelta(hours=24)


def test_conflicting_replay_is_rejected_without_replacing_ciphertext(
    tmp_path: Path,
) -> None:
    """Only an identical completion replay is idempotent."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    first = _for_challenge(_vector_submission(), challenge)
    service = KeyCeremonyService(store)
    service.complete(job_id, "adepthood", first, now=_NOW)

    raw = first.model_dump(mode="json")
    raw["wrapped_artifact"]["binding"]["client_nonce"] = "B" * 43
    conflicting = CeremonySubmission.model_validate(raw)
    with pytest.raises(CeremonyConflictError):
        service.complete(job_id, "adepthood", conflicting, now=_NOW)

    assert store.get_wrapped_key_artifact(job_id, "adepthood") == first.wrapped_artifact


def test_expired_ceremony_queues_idempotent_provider_teardown(tmp_path: Path) -> None:
    """Timeout makes the allocation unusable and reconciles its resources to zero."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    submission = _for_challenge(_vector_submission(), challenge)
    service = KeyCeremonyService(store)

    with pytest.raises(CeremonyExpiredError):
        service.complete(job_id, "adepthood", submission, now=challenge.expires_at)
    assert store.expire_key_ceremonies(now=challenge.expires_at) == 0

    driver = FakeProviderDriver()
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    assert worker.run_once(now=challenge.expires_at) is True
    deleted = store.get(job_id, "adepthood")
    assert deleted is not None
    assert deleted.state is JobState.DELETED
    assert driver.delete_count == 1


def test_cancelled_ceremony_retains_no_artifact_and_tears_down_once(
    tmp_path: Path,
) -> None:
    """Cancellation queues the empty allocation for idempotent reconciliation."""
    store, job_id = _awaiting_store(tmp_path)
    first = store.request_delete(job_id, "adepthood", now=_NOW)
    repeated = store.request_delete(job_id, "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())

    assert first == repeated
    assert first.state is JobState.DELETING
    assert store.get_wrapped_key_artifact(job_id, "adepthood") is None
    assert worker.run_once(now=_NOW) is True
    assert worker.run_once(now=_NOW) is False
    assert driver.delete_count == 1


def test_attestation_expiry_fails_before_any_key_release(tmp_path: Path) -> None:
    """Even a correctly signed but expired quote cannot release the opaque envelope."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    submission = _for_challenge(_vector_submission(), challenge)
    root = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate().public_key()
    statement = _attestation(
        challenge,
        root,
        recipient,
        issued_at=_NOW - timedelta(minutes=2),
        expires_at=_NOW - timedelta(minutes=1),
    )
    envelope = _release_envelope(recipient)
    sink = FakeKeyReleaseSink()
    verifier = Ed25519AttestationVerifier(
        expected_measurement="sha384:expected",
        trust_root=root.public_key(),
    )
    service = KeyCeremonyService(store, verifier=verifier, release_sink=sink)

    with pytest.raises(CeremonyConflictError, match="attestation"):
        service.complete(
            job_id,
            "adepthood",
            _with_attested_release(submission, statement, envelope),
            now=_NOW,
        )

    assert sink.delivery_count == 0
    awaiting = store.get(job_id, "adepthood")
    assert awaiting is not None
    assert awaiting.state is JobState.AWAITING_KEY_CEREMONY


@pytest.mark.parametrize(
    "defect",
    ["measurement", "challenge", "signature", "future", "overlong", "recipient"],
)
def test_every_attestation_failure_prevents_key_release(
    tmp_path: Path,
    defect: str,
) -> None:
    """Every measured-recipient trust check fails closed before the sink."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    root = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate().public_key()
    statement = _attestation(
        challenge,
        root,
        recipient,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    envelope = _release_envelope(recipient)
    statement_raw = statement.model_dump(mode="json")
    if defect == "measurement":
        statement_raw["measurement"] = "sha384:wrong"
    elif defect == "challenge":
        statement_raw["challenge_nonce"] = "C" * 43
    elif defect == "signature":
        statement_raw["signature"] = _b64(bytes(64))
    elif defect == "future":
        statement_raw["issued_at"] = (_NOW + timedelta(minutes=1)).isoformat()
    elif defect == "overlong":
        statement_raw["expires_at"] = (
            challenge.expires_at + timedelta(seconds=1)
        ).isoformat()
    else:
        envelope = _release_envelope(X25519PrivateKey.generate().public_key())
    invalid_statement = AttestationStatement.model_validate(statement_raw)
    sink = FakeKeyReleaseSink()
    service = KeyCeremonyService(
        store,
        verifier=Ed25519AttestationVerifier(
            expected_measurement="sha384:expected",
            trust_root=root.public_key(),
        ),
        release_sink=sink,
    )
    submission = _with_attested_release(
        _for_challenge(_vector_submission(), challenge),
        invalid_statement,
        envelope,
    )

    with pytest.raises(CeremonyConflictError):
        service.complete(job_id, "adepthood", submission, now=_NOW)

    assert sink.delivery_count == 0
    awaiting = store.get(job_id, "adepthood")
    assert awaiting is not None
    assert awaiting.state is JobState.AWAITING_KEY_CEREMONY


def test_attested_release_is_verified_then_delivered_once(tmp_path: Path) -> None:
    """A fresh measured quote authorizes one opaque, idempotent enclave release."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    submission = _for_challenge(_vector_submission(), challenge)
    root = Ed25519PrivateKey.generate()
    recipient = X25519PrivateKey.generate().public_key()
    statement = _attestation(
        challenge,
        root,
        recipient,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
    )
    assert (
        root.public_key().verify(
            statement.signature_bytes,
            attestation_signed_payload(statement),
        )
        is None
    )
    envelope = _release_envelope(recipient)
    sink = FakeKeyReleaseSink()
    service = KeyCeremonyService(
        store,
        verifier=Ed25519AttestationVerifier(
            expected_measurement="sha384:expected",
            trust_root=root.public_key(),
        ),
        release_sink=sink,
    )
    attested = _with_attested_release(submission, statement, envelope)

    completed = service.complete(job_id, "adepthood", attested, now=_NOW)
    repeated = service.complete(job_id, "adepthood", attested, now=_NOW)

    assert completed == repeated
    assert completed.attested_confidential is True
    assert sink.delivery_count == 1


def test_database_never_contains_plaintext_user_key_material(tmp_path: Path) -> None:
    """Only the wrapped artifact is durable; all three user secrets remain absent."""
    store, job_id = _awaiting_store(tmp_path)
    challenge = store.get_key_ceremony(job_id, "adepthood")
    KeyCeremonyService(store).complete(
        job_id,
        "adepthood",
        _for_challenge(_vector_submission(), challenge),
        now=_NOW,
    )
    database = (tmp_path / "provisioning.sqlite3").read_bytes()

    assert _PASSPHRASE.encode() not in database
    assert _RECOVERY.encode() not in database
    assert _VMK.hex().encode() not in database
    assert KEY_CEREMONY_VERSION.encode() in database


def test_http_challenge_and_completion_publish_only_safe_resumable_state(
    tmp_path: Path,
) -> None:
    """The API exposes a challenge and honest capability, never recovery data."""
    store, job_id = _awaiting_store(tmp_path)
    client = _api_client(store)
    path = f"/control/v1/jobs/{job_id}/key-ceremony"

    challenge_response = client.get(path, headers=_headers())
    challenge = KeyCeremonyChallenge.model_validate(challenge_response.json())
    submission = _for_challenge(_vector_submission(), challenge)
    completed = client.put(
        path,
        headers=_headers(),
        json=submission.model_dump(mode="json"),
    )

    assert challenge_response.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["state"] == JobState.READY.value
    assert completed.json()["attested_confidential"] is False
    assert completed.headers["Cache-Control"] == "no-store"
    assert "recovery" not in challenge_response.text.lower()
    assert "recovery" not in completed.text.lower()
    assert client.get(path, headers=_headers()).json()["code"] == "invalid_transition"


def test_http_rejects_secret_fields_and_cross_consumer_access(tmp_path: Path) -> None:
    """The wire boundary forbids secrets and hides another consumer's ceremony."""
    store, job_id = _awaiting_store(tmp_path)
    client = _api_client(store)
    path = f"/control/v1/jobs/{job_id}/key-ceremony"
    challenge = KeyCeremonyChallenge.model_validate(
        client.get(path, headers=_headers()).json()
    )
    raw = _for_challenge(_vector_submission(), challenge).model_dump(mode="json")
    raw["recovery_code"] = _RECOVERY

    invalid = client.put(path, headers=_headers(), json=raw)
    foreign = client.get(path, headers=_headers(_OTHER_TOKEN))

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_request"
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "job_unavailable"
