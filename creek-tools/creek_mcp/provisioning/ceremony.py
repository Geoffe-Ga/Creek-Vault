"""Versioned user-held key ceremony with replay and attestation gates (#1771)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from creek_mcp.provisioning.models import ProvisioningJob
    from creek_mcp.provisioning.store import ProvisioningStore

KEY_CEREMONY_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
KEY_CEREMONY_TTL: Final[timedelta] = timedelta(hours=24)
_ATTESTATION_DOMAIN: Final[bytes] = b"creek.key-ceremony.attestation.v1\0"
_IDENTIFIER = Annotated[str, Field(min_length=1, max_length=200)]
_HEX_12 = Annotated[str, Field(pattern=r"^[0-9a-f]{24}$")]
_HEX_16 = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
_HEX_48 = Annotated[str, Field(pattern=r"^[0-9a-f]{96}$")]
_BASE64_12 = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16}$")]
_BASE64_32 = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
_BASE64_48 = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{64}$")]
_BASE64_64 = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{86}$")]


class CeremonyConflictError(RuntimeError):
    """A ceremony request is invalid, conflicting, or fails attestation."""


class CeremonyExpiredError(RuntimeError):
    """The ceremony expired and its allocation has been queued for teardown."""


class CeremonyUnavailableError(RuntimeError):
    """The owned job has no ceremony available in its current state."""


class CeremonyBinding(BaseModel):
    """Public context authenticated by both wrapped VMK copies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0.0"]
    activation_id: _IDENTIFIER
    ceremony_id: _IDENTIFIER
    server_nonce: _BASE64_32
    client_nonce: _BASE64_32


class WrappedCiphertext(BaseModel):
    """One AES-256-GCM nonce and wrapped 256-bit VMK."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nonce: _HEX_12
    ciphertext: _HEX_48


class Argon2idParameters(BaseModel):
    """Fixed version-1 passphrase derivation parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["argon2id"]
    salt: _HEX_16
    time_cost: Literal[3]
    lanes: Literal[4]
    memory_kib: Literal[65536]


class WrappedKeyArtifact(BaseModel):
    """Ciphertext-only version-2 key vault accepted from a user client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2]
    kdf: Argon2idParameters
    passphrase_wrapped: WrappedCiphertext
    recovery_wrapped: WrappedCiphertext
    binding: CeremonyBinding


class KeyCeremonyChallenge(BaseModel):
    """Fresh public server challenge for exactly one durable activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0.0"] = KEY_CEREMONY_VERSION
    job_id: _IDENTIFIER
    activation_id: _IDENTIFIER
    ceremony_id: _IDENTIFIER
    server_nonce: _BASE64_32
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        """Reject ambiguous wall-clock expiries."""
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value


class AttestationStatement(BaseModel):
    """Public measured-recipient statement signed by the configured trust root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["creek-ed25519-x25519-v1"]
    measurement: _IDENTIFIER
    challenge_nonce: _BASE64_32
    recipient_public_key: _BASE64_32
    issued_at: datetime
    expires_at: datetime
    signature: _BASE64_64

    @model_validator(mode="after")
    def _valid_window(self) -> AttestationStatement:
        """Require one finite, timezone-aware attestation window."""
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("attestation timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("attestation expiry must follow issuance")
        return self

    @property
    def signature_bytes(self) -> bytes:
        """Return the decoded Ed25519 signature."""
        return _urlsafe_decode(self.signature)


class KeyReleaseEnvelope(BaseModel):
    """Opaque VMK envelope addressed to the attested X25519 recipient."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["x25519-hkdf-sha256-aes256gcm"]
    recipient_public_key: _BASE64_32
    ephemeral_public_key: _BASE64_32
    nonce: _BASE64_12
    ciphertext: _BASE64_48


class CeremonySubmission(BaseModel):
    """Strict ciphertext-only client completion request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0.0"]
    ceremony_id: _IDENTIFIER
    server_nonce: _BASE64_32
    recovery_saved: Literal[True]
    wrapped_artifact: WrappedKeyArtifact
    attestation: AttestationStatement | None
    key_release: KeyReleaseEnvelope | None

    @field_validator("recovery_saved", mode="before")
    @classmethod
    def _literal_confirmation(cls, value: object) -> object:
        """Reject truthy coercions; the acknowledgement must be JSON true."""
        if value is not True:
            raise ValueError("recovery_saved must be exactly true")
        return value

    @model_validator(mode="after")
    def _paired_attestation_release(self) -> CeremonySubmission:
        """Require attestation and its addressed release envelope together."""
        if (self.attestation is None) != (self.key_release is None):
            raise ValueError("attestation and key_release must be supplied together")
        return self

    def canonical_json(self) -> str:
        """Return the deterministic persistence and replay-fingerprint form."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


class AttestationVerifier(Protocol):
    """Verify a measured recipient against one fresh ceremony challenge."""

    def verify(
        self,
        statement: AttestationStatement,
        challenge: KeyCeremonyChallenge,
        *,
        now: datetime,
    ) -> bool:
        """Return whether *statement* authorizes release at *now*."""


class KeyReleaseSink(Protocol):
    """Idempotently deliver one opaque VMK envelope to an attested runtime."""

    def deliver(
        self,
        ceremony_id: str,
        job_id: str,
        envelope: KeyReleaseEnvelope,
    ) -> None:
        """Deliver or acknowledge one byte-identical prior envelope."""


def _urlsafe_decode(value: str) -> bytes:
    """Decode one unpadded URL-safe base64 value."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def attestation_signed_payload(statement: AttestationStatement) -> bytes:
    """Return the language-neutral canonical Ed25519 statement payload."""
    public = statement.model_dump(mode="json", exclude={"signature"})
    return _ATTESTATION_DOMAIN + json.dumps(
        public,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


class Ed25519AttestationVerifier:
    """Verify freshness, challenge binding, measurement, and root signature."""

    def __init__(
        self,
        *,
        expected_measurement: str,
        trust_root: Ed25519PublicKey,
    ) -> None:
        """Bind verification to one expected image and configured public root."""
        self._expected_measurement = expected_measurement
        self._trust_root = trust_root

    def verify(
        self,
        statement: AttestationStatement,
        challenge: KeyCeremonyChallenge,
        *,
        now: datetime,
    ) -> bool:
        """Return true only for one live, correctly measured signed recipient."""
        if statement.measurement != self._expected_measurement:
            return False
        if statement.challenge_nonce != challenge.server_nonce:
            return False
        if not statement.issued_at <= now < statement.expires_at:
            return False
        if statement.expires_at > challenge.expires_at:
            return False
        try:
            self._trust_root.verify(
                statement.signature_bytes,
                attestation_signed_payload(statement),
            )
        except InvalidSignature:
            return False
        return True


class FakeKeyReleaseSink:
    """Secret-free idempotent key-release sink for contract tests."""

    def __init__(self) -> None:
        """Initialize an empty envelope-fingerprint ledger."""
        self._lock = Lock()
        self._fingerprints: dict[str, bytes] = {}

    @property
    def delivery_count(self) -> int:
        """Return the number of distinct ceremony deliveries."""
        with self._lock:
            return len(self._fingerprints)

    def deliver(
        self,
        ceremony_id: str,
        job_id: str,
        envelope: KeyReleaseEnvelope,
    ) -> None:
        """Accept one opaque envelope and reject a conflicting replay."""
        payload = f"{job_id}\0{envelope.model_dump_json()}".encode()
        fingerprint = hashlib.sha256(payload).digest()
        with self._lock:
            existing = self._fingerprints.get(ceremony_id)
            if existing is None:
                self._fingerprints[ceremony_id] = fingerprint
                return
            if existing != fingerprint:
                raise CeremonyConflictError("key release conflicts with prior delivery")


class KeyCeremonyService:
    """Apply attestation policy before atomically completing a durable ceremony."""

    def __init__(
        self,
        store: ProvisioningStore,
        *,
        verifier: AttestationVerifier | None = None,
        release_sink: KeyReleaseSink | None = None,
    ) -> None:
        """Bind durable state to optional attested-release dependencies."""
        self._store = store
        self._verifier = verifier
        self._release_sink = release_sink

    def complete(
        self,
        job_id: str,
        requester_identity: str,
        submission: CeremonySubmission,
        now: datetime,
    ) -> ProvisioningJob:
        """Complete an ordinary or verified-attested key ceremony idempotently."""
        challenge = self._store.get_key_ceremony(job_id, requester_identity)
        before_settle, attested = self._release_gate(
            job_id,
            challenge,
            submission,
            now=now,
        )
        return self._store.complete_key_ceremony(
            job_id,
            requester_identity,
            submission,
            attested_confidential=attested,
            before_settle=before_settle,
            now=now,
        )

    def _release_gate(
        self,
        job_id: str,
        challenge: KeyCeremonyChallenge,
        submission: CeremonySubmission,
        *,
        now: datetime,
    ) -> tuple[Callable[[], None] | None, bool]:
        """Return the settlement hook only after attestation passes."""
        statement = submission.attestation
        envelope = submission.key_release
        if statement is None or envelope is None:
            return None, False
        if self._verifier is None or self._release_sink is None:
            raise CeremonyConflictError("attestation is unavailable")
        if envelope.recipient_public_key != statement.recipient_public_key:
            raise CeremonyConflictError("attestation recipient does not match release")
        if not self._verifier.verify(statement, challenge, now=now):
            raise CeremonyConflictError("attestation verification failed")

        def release() -> None:
            assert self._release_sink is not None
            self._release_sink.deliver(challenge.ceremony_id, job_id, envelope)

        return release, True
