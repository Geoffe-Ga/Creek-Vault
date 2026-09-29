"""Production secret and callback adapters for provisioning issue #1805."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from creek_mcp.provisioning import production_secrets
from creek_mcp.provisioning.driver import FakeProviderDriver, HandoffError
from creek_mcp.provisioning.handoff import HttpOneTimeCredentialHandoff
from creek_mcp.provisioning.models import FailureReason, JobState
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    EncryptedFileRoutingCredentialVerifier,
)
from creek_mcp.provisioning.routing import RoutingPrincipal
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_support import (
    PROVIDER_TOKEN,
    FakeFlyAPI,
    fly_driver_from_file,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
_CONSUMER = "adepthood-user-001"
_ACTIVATION = "activation-production-001"
_REQUESTER = "adepthood"


def _owner_file(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def _ca_files(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Creek test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(_NOW - timedelta(days=1))
        .not_valid_after(_NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = _owner_file(
        tmp_path / "ca.crt", cert.public_bytes(serialization.Encoding.PEM)
    )
    key_path = _owner_file(
        tmp_path / "ca.key",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return cert_path, key_path


def _secret_manager(tmp_path: Path) -> EncryptedFileFlySecretManager:
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    master = _owner_file(tmp_path / "master-key", b"m" * 32)
    cert, key = _ca_files(tmp_path)
    return EncryptedFileFlySecretManager(
        state,
        master_key_file=master,
        ca_certificate_file=cert,
        ca_private_key_file=key,
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    )


def test_runtime_secret_issue_is_restart_safe_encrypted_and_revocable(
    tmp_path: Path,
) -> None:
    """A replay returns one bundle while disk contains no plaintext credential."""
    first_manager = _secret_manager(tmp_path)
    first = first_manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    second = first_manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    restarted = EncryptedFileFlySecretManager(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    ).issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    assert first == second == restarted
    assert first.consumer_registry == (
        f"{_CONSUMER}={first.consumer_credential}\n".encode()
    )
    assert len(first.replay_state) >= 43
    assert first.replay_state != first.consumer_credential
    certificate = x509.load_pem_x509_certificate(first.tls_certificate)
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert names.get_values_for_type(x509.DNSName)[0].startswith("*.vm.creek-vault-")
    persisted = b"".join(
        path.read_bytes() for path in (tmp_path / "runtime-secrets").iterdir()
    )
    assert first.consumer_credential.encode() not in persisted
    assert first.replay_state.encode() not in persisted
    assert first.tls_private_key not in persisted

    first_manager.revoke(_ACTIVATION)
    first_manager.revoke(_ACTIVATION)
    with pytest.raises(ValueError, match="revoked"):
        first_manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)


@pytest.mark.asyncio
async def test_pre_replay_encrypted_bundle_remains_restart_and_router_compatible(
    tmp_path: Path,
) -> None:
    """Adding replay state cannot strand an existing ordinary TLS allocation."""
    manager = _secret_manager(tmp_path)
    credential = "legacy-consumer-credential-canary"
    document = {
        "activation_id": _ACTIVATION,
        "requester_identity": _REQUESTER,
        "consumer_identity": _CONSUMER,
        "consumer_credential": credential,
        "consumer_registry": production_secrets._b64(
            f"{_CONSUMER}={credential}\n".encode()
        ),
        "tls_certificate": production_secrets._b64(b"legacy-certificate"),
        "tls_private_key": production_secrets._b64(b"legacy-private-key"),
    }
    digest = production_secrets._activation_digest(_ACTIVATION)
    nonce = b"n" * 12
    encrypted = nonce + AESGCM(b"m" * 32).encrypt(
        nonce,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        production_secrets._aad(digest),
    )
    bundle_path = tmp_path / "runtime-secrets" / f"{digest}.bundle"
    bundle_path.write_bytes(encrypted)
    bundle_path.chmod(0o600)

    first = manager.issue(
        _ACTIVATION,
        _CONSUMER,
        requester_identity=_REQUESTER,
    )
    restarted = EncryptedFileFlySecretManager(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    ).issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    verifier = EncryptedFileRoutingCredentialVerifier(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
    )

    assert first == restarted
    assert first.consumer_credential == credential
    assert len(first.replay_state) == 64
    assert await verifier.verify_credential(credential) == RoutingPrincipal(
        _REQUESTER,
        _CONSUMER,
        first.replay_state,
    )


def test_runtime_secret_manager_refuses_unsafe_files_and_identity(
    tmp_path: Path,
) -> None:
    """Mounted secrets and registry identities fail closed before issuance."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    master = _owner_file(tmp_path / "master-key", b"m" * 32)
    cert, key = _ca_files(tmp_path)
    master.chmod(0o640)
    with pytest.raises(ValueError, match="owner-only"):
        EncryptedFileFlySecretManager(
            state,
            master_key_file=master,
            ca_certificate_file=cert,
            ca_private_key_file=key,
            app_prefix="creek-vault",
        )
    master.chmod(0o600)
    manager = EncryptedFileFlySecretManager(
        state,
        master_key_file=master,
        ca_certificate_file=cert,
        ca_private_key_file=key,
        app_prefix="creek-vault",
    )
    with pytest.raises(ValueError, match="consumer identity"):
        manager.issue(
            _ACTIVATION,
            "line\nbreak",
            requester_identity=_REQUESTER,
        )


def test_runtime_secret_decode_suppresses_secret_bearing_parse_error(
    tmp_path: Path,
) -> None:
    """Malformed authenticated plaintext cannot survive in an exception cause."""
    manager = _secret_manager(tmp_path)
    manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    bundle_path = next((tmp_path / "runtime-secrets").glob("*.bundle"))
    nonce = bundle_path.read_bytes()[:12]
    canary = "decrypted-runtime-secret-canary"
    malformed = f'{{"consumer_credential":"{canary}"'.encode()
    associated_data = production_secrets._aad(
        production_secrets._activation_digest(_ACTIVATION)
    )
    bundle_path.write_bytes(
        nonce + AESGCM(b"m" * 32).encrypt(nonce, malformed, associated_data)
    )

    with pytest.raises(ValueError, match="runtime secret state is invalid") as raised:
        manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    assert raised.value.__cause__ is None
    assert canary not in str(raised.value)


@pytest.mark.asyncio
async def test_routing_verifier_binds_exact_encrypted_owner_after_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted read-only verifier returns only the issued owner pair."""
    manager = _secret_manager(tmp_path)
    issued = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    comparisons: list[tuple[bytes, bytes]] = []
    compare_digest = hmac.compare_digest

    def tracked_compare(left: bytes, right: bytes) -> bool:
        comparisons.append((left, right))
        return compare_digest(left, right)

    monkeypatch.setattr(production_secrets.hmac, "compare_digest", tracked_compare)
    verifier = EncryptedFileRoutingCredentialVerifier(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
    )
    with caplog.at_level(logging.DEBUG):
        principal = await verifier.verify_credential(issued.consumer_credential)
        unknown = await verifier.verify_credential("unknown-routing-credential")

    assert principal == RoutingPrincipal(_REQUESTER, _CONSUMER, issued.replay_state)
    assert unknown is None
    assert len(comparisons) == 2
    persisted = b"".join(
        path.read_bytes() for path in (tmp_path / "runtime-secrets").iterdir()
    )
    rendered = repr(verifier) + caplog.text
    for secret in (
        issued.consumer_credential,
        issued.replay_state,
        _REQUESTER,
        _CONSUMER,
        _ACTIVATION,
    ):
        assert secret.encode() not in persisted
        assert secret not in rendered


@pytest.mark.asyncio
async def test_routing_verifier_refuses_revoked_and_conflicting_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation and duplicate credential ownership both fail closed."""
    manager = _secret_manager(tmp_path)
    issued = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    verifier = EncryptedFileRoutingCredentialVerifier(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
    )
    manager.revoke(_ACTIVATION)
    assert await verifier.verify_credential(issued.consumer_credential) is None

    original_urandom = production_secrets.os.urandom

    def repeated_credential(size: int) -> bytes:
        return b"c" * size if size == 32 else original_urandom(size)

    monkeypatch.setattr(production_secrets.os, "urandom", repeated_credential)
    first = manager.issue(
        "activation-conflict-one",
        "consumer-one",
        requester_identity="requester-one",
    )
    second = manager.issue(
        "activation-conflict-two",
        "consumer-two",
        requester_identity="requester-two",
    )
    assert first.consumer_credential == second.consumer_credential
    assert await verifier.verify_credential(first.consumer_credential) is None


@pytest.mark.asyncio
async def test_routing_verifier_refuses_tampered_state_without_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt ciphertext becomes an anonymous miss with no secret diagnostic."""
    manager = _secret_manager(tmp_path)
    issued = manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)
    bundle_path = next((tmp_path / "runtime-secrets").glob("*.bundle"))
    encrypted = bundle_path.read_bytes()
    bundle_path.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
    verifier = EncryptedFileRoutingCredentialVerifier(
        tmp_path / "runtime-secrets",
        master_key_file=tmp_path / "master-key",
    )

    with caplog.at_level(logging.DEBUG):
        assert await verifier.verify_credential(issued.consumer_credential) is None

    assert issued.consumer_credential not in caplog.text


def test_revocation_wins_a_race_with_bundle_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issuance cannot resurrect a bundle after its tombstone is durable."""
    manager = _secret_manager(tmp_path)
    exclusive_write = production_secrets._exclusive_write

    def revoke_before_bundle_write(path: Path, value: bytes) -> None:
        if path.suffix == ".bundle":
            manager.revoke(_ACTIVATION)
        exclusive_write(path, value)

    monkeypatch.setattr(
        production_secrets,
        "_exclusive_write",
        revoke_before_bundle_write,
    )

    with pytest.raises(ValueError, match="revoked"):
        manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)

    state_files = tuple((tmp_path / "runtime-secrets").iterdir())
    assert any(path.suffix == ".revoked" for path in state_files)
    assert all(path.suffix != ".bundle" for path in state_files)


def test_handoff_posts_authenticated_payload_without_retaining_plaintext(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production callback sends one secret-bearing HTTPS request only."""
    bearer = "handoff-bearer-canary"
    credential = "consumer-credential-canary"
    token_file = _owner_file(tmp_path / "handoff-token", bearer.encode())
    seen: list[httpx.Request] = []

    def accept(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204, request=request)

    client = httpx.Client(transport=httpx.MockTransport(accept))
    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=client,
    )
    with caplog.at_level(logging.DEBUG):
        handoff.deliver("job-001", _CONSUMER, "https://vault.internal/v1", credential)

    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Bearer {bearer}"
    assert json.loads(seen[0].content) == {
        "job_id": "job-001",
        "consumer_identity": _CONSUMER,
        "vault_url": "https://vault.internal/v1",
        "consumer_credential": credential,
    }
    rendered = repr(handoff) + caplog.text
    assert bearer not in rendered
    assert credential not in rendered


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(409, False), (400, False), (408, True), (429, True), (503, True)],
)
def test_handoff_classifies_refusals_without_echoing_response(
    tmp_path: Path,
    status: int,
    retryable: bool,
) -> None:
    """Only transient HTTP outcomes return to the durable retry lane."""
    token_file = _owner_file(tmp_path / "handoff-token", b"mounted-bearer")
    response_canary = "response-secret-canary"

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=response_canary, request=request)

    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(HandoffError) as raised:
        handoff.deliver("job-001", _CONSUMER, "https://vault.internal/v1", "secret")

    assert raised.value.retryable is retryable
    assert response_canary not in str(raised.value)


def test_handoff_closes_a_refusal_without_reading_its_body(tmp_path: Path) -> None:
    """Only the status is consumed, so an unbounded response cannot enter memory."""
    token_file = _owner_file(tmp_path / "handoff-token", b"mounted-bearer")
    closed: list[bool] = []

    class UnreadableBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("handoff response body was read")

        def close(self) -> None:
            closed.append(True)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=UnreadableBody(), request=request)

    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )

    with pytest.raises(HandoffError) as raised:
        handoff.deliver("job-001", _CONSUMER, "https://vault.internal/v1", "secret")

    assert raised.value.retryable is True
    assert closed == [True]


def test_handoff_timeout_is_retryable_and_plaintext_transport_is_refused(
    tmp_path: Path,
) -> None:
    """A bounded transport timeout retries; HTTP can never carry the bearer."""
    token_file = _owner_file(tmp_path / "handoff-token", b"mounted-bearer")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret-bearing timeout", request=request)

    with pytest.raises(ValueError, match="HTTPS"):
        HttpOneTimeCredentialHandoff(
            "http://adepthood.test/internal/vault-provisioning/completions",
            token_file,
        )
    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(HandoffError) as raised:
        handoff.deliver("job-001", _CONSUMER, "https://vault.internal/v1", "secret")
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    "canary",
    [
        b"handoff-bearer-canary-\xff",
        "handoff-bearer-canary-\u00e9".encode(),
    ],
)
def test_handoff_validates_mounted_bearer_without_leaking_decode_detail(
    tmp_path: Path,
    canary: bytes,
) -> None:
    """Readiness rejects an invalid bearer and suppresses its decoding cause."""
    token_file = _owner_file(tmp_path / "handoff-token", canary)

    with pytest.raises(ValueError, match="handoff bearer file is invalid") as raised:
        HttpOneTimeCredentialHandoff(
            "https://adepthood.test/internal/vault-provisioning/completions",
            token_file,
        )

    assert raised.value.__cause__ is None
    assert "handoff-bearer-canary" not in str(raised.value)


def test_transient_callback_failure_enters_the_durable_retry_lane(
    tmp_path: Path,
) -> None:
    """The HTTP adapter's retry classification reaches the worker store."""
    token_file = _owner_file(tmp_path / "handoff-token", b"mounted-bearer")

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    job = store.submit(_ACTIVATION, _CONSUMER, now=_NOW)

    assert ProvisioningWorker(store, FakeProviderDriver(), handoff).run_once(now=_NOW)
    failed = store.get(job.job_id, _CONSUMER)

    assert failed is not None
    assert failed.state is JobState.FAILED
    assert failed.failure_reason is FailureReason.HANDOFF_FAILED
    assert failed.retryable is True


@pytest.mark.integration
def test_real_adapters_reconcile_full_fake_provider_and_callback_lifecycle(
    tmp_path: Path,
) -> None:
    """Production adapters replay create/handoff/delete without duplicates."""
    api = FakeFlyAPI()
    secrets = _secret_manager(tmp_path)
    token_file = _owner_file(tmp_path / "handoff-token", b"mounted-bearer")
    deliveries: dict[str, bytes] = {}

    def adepthood(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer mounted-bearer"
        payload = json.loads(request.content)
        encoded = json.dumps(payload, sort_keys=True).encode()
        fingerprint = hashlib.sha256(encoded).digest()
        existing = deliveries.get(payload["job_id"])
        if existing is not None and existing != fingerprint:
            return httpx.Response(409, request=request)
        deliveries[payload["job_id"]] = fingerprint
        return httpx.Response(204, request=request)

    handoff = HttpOneTimeCredentialHandoff(
        "https://adepthood.test/internal/vault-provisioning/completions",
        token_file,
        client=httpx.Client(transport=httpx.MockTransport(adepthood)),
    )
    database = tmp_path / "production.sqlite3"
    store = ProvisioningStore(database)
    job = store.submit(_ACTIVATION, _CONSUMER, now=_NOW)
    fly_token_file = _owner_file(tmp_path / "fly-token", PROVIDER_TOKEN.encode())
    driver = fly_driver_from_file(api, fly_token_file, secrets)
    worker = ProvisioningWorker(store, driver, handoff)

    api.fail_once("POST", "/machines", status=401)
    assert worker.run_once(now=_NOW)
    failed = store.get(job.job_id, _CONSUMER)
    assert failed is not None and failed.retryable
    assert failed.failure_reason is FailureReason.PROVIDER_REJECTED
    assert len(api.apps) == 1
    assert sum(map(len, api.volumes.values())) == 1
    assert sum(map(len, api.machines.values())) == 0
    assert deliveries == {}
    create_rotation = "rotated-fly-provider-create-canary"
    api.provider_token = create_rotation
    _owner_file(fly_token_file, create_rotation.encode())
    store.retry(job.job_id, _CONSUMER, now=_NOW)
    driver = fly_driver_from_file(api, fly_token_file, secrets)
    worker = ProvisioningWorker(store, driver, handoff)
    assert worker.run_once(now=_NOW)
    allocation = driver.provision(job)
    assert driver.provision(job) == allocation
    handoff.deliver(
        job.job_id,
        _CONSUMER,
        allocation.vault_url,
        allocation.consumer_credential,
    )
    with pytest.raises(HandoffError) as conflict:
        handoff.deliver(
            job.job_id,
            _CONSUMER,
            allocation.vault_url,
            allocation.consumer_credential + "-conflict",
        )
    assert conflict.value.retryable is False
    assert len(api.apps) == 1
    assert sum(map(len, api.volumes.values())) == 1
    assert sum(map(len, api.machines.values())) == 1
    store.request_delete(job.job_id, _CONSUMER, now=_NOW)
    api.fail_once("DELETE", "/volumes/vol-1", status=401)
    assert worker.run_once(now=_NOW)
    failed = store.get(job.job_id, _CONSUMER)
    assert failed is not None and failed.retryable
    assert failed.failure_reason is FailureReason.PROVIDER_REJECTED
    delete_rotation = "rotated-fly-provider-delete-canary"
    api.provider_token = delete_rotation
    _owner_file(fly_token_file, delete_rotation.encode())
    store.retry(job.job_id, _CONSUMER, now=_NOW)
    worker = ProvisioningWorker(
        store,
        fly_driver_from_file(api, fly_token_file, secrets),
        handoff,
    )
    assert worker.run_once(now=_NOW)

    deleted = store.get(job.job_id, _CONSUMER)
    assert deleted is not None and deleted.state is JobState.DELETED
    assert api.apps == {}
    assert len(deliveries) == 1
    persisted = database.read_bytes() + b"".join(
        path.read_bytes() for path in (tmp_path / "runtime-secrets").iterdir()
    )
    for canary in (
        allocation.consumer_credential,
        create_rotation,
        delete_rotation,
    ):
        assert canary.encode() not in persisted
