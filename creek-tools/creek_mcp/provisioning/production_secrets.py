"""Encrypted, restart-safe Fly runtime-secret issuance for issue #1805."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import FlyRuntimeSecrets
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.replay_contract import REPLAY_STATE_BYTES, is_replay_state
from creek_mcp.provisioning.routing import RoutingPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryptography.hazmat.primitives.asymmetric.types import (
        CertificateIssuerPrivateKeyTypes,
    )

_MAX_SECRET_FILE_BYTES: Final[int] = 64 * 1024
_MASTER_KEY_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 12
_SAFE_IDENTITY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:@/+\-]{1,255}")
_SAFE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_ALLOCATION_DIGEST_LENGTH: Final[int] = 24
_CERTIFICATE_VALIDITY: Final[timedelta] = timedelta(days=30)
_BUNDLE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{24}")


def read_owner_only_file(path: Path, *, maximum: int = _MAX_SECRET_FILE_BYTES) -> bytes:
    """Read one bounded owner-only regular file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("secret file is unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("secret file must be owner-only and regular")
        value = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if not value or len(value) > maximum:
        raise ValueError("secret file is empty or too large")
    return value


@dataclass(frozen=True, slots=True)
class _BundleDocument:
    """Serializable plaintext that exists only while issuing one bundle."""

    activation_id: str = field(repr=False)
    requester_identity: str = field(repr=False)
    consumer_identity: str = field(repr=False)
    consumer_credential: str = field(repr=False)
    replay_state: str = field(repr=False)
    consumer_registry: bytes = field(repr=False)
    tls_certificate: bytes = field(repr=False)
    tls_private_key: bytes = field(repr=False)

    def encode(self) -> bytes:
        """Encode the in-memory bundle for authenticated encryption."""
        document = {
            "activation_id": self.activation_id,
            "requester_identity": self.requester_identity,
            "consumer_identity": self.consumer_identity,
            "consumer_credential": self.consumer_credential,
            "replay_state": self.replay_state,
            "consumer_registry": _b64(self.consumer_registry),
            "tls_certificate": _b64(self.tls_certificate),
            "tls_private_key": _b64(self.tls_private_key),
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def decode(cls, value: bytes) -> _BundleDocument:
        """Decode a previously authenticated bundle document."""
        try:
            document = json.loads(value)
            if not isinstance(document, dict):
                raise ValueError
            activation = document["activation_id"]
            requester = document["requester_identity"]
            consumer = document["consumer_identity"]
            credential = document["consumer_credential"]
            replay_state = document.get("replay_state")
            if (
                replay_state is None
                and isinstance(activation, str)
                and isinstance(credential, str)
            ):
                replay_state = _legacy_replay_state(activation, credential)
            if (
                not isinstance(activation, str)
                or not activation.strip()
                or not isinstance(requester, str)
                or _SAFE_IDENTITY_RE.fullmatch(requester) is None
                or not isinstance(consumer, str)
                or _SAFE_IDENTITY_RE.fullmatch(consumer) is None
                or not isinstance(credential, str)
                or not credential
                or not is_replay_state(replay_state)
            ):
                raise ValueError
            return cls(
                activation,
                requester,
                consumer,
                credential,
                replay_state,
                _unb64(document["consumer_registry"]),
                _unb64(document["tls_certificate"]),
                _unb64(document["tls_private_key"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("runtime secret state is invalid") from None

    def runtime(self) -> FlyRuntimeSecrets:
        """Return the provider-facing repr-safe secret bundle."""
        return FlyRuntimeSecrets(
            self.consumer_credential,
            self.replay_state,
            self.consumer_registry,
            self.tls_certificate,
            self.tls_private_key,
        )


class EncryptedFileFlySecretManager:
    """Issue one CA-signed encrypted runtime bundle per activation.

    The state directory contains AES-GCM ciphertext and content-free revoked
    tombstones only. A successful atomic write precedes any bundle leaving this
    boundary, so a process restart always returns the exact prior plaintext.
    """

    def __init__(
        self,
        state_directory: Path,
        *,
        master_key_file: Path,
        ca_certificate_file: Path,
        ca_private_key_file: Path,
        app_prefix: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Load mounted key material and validate the owner-only state root."""
        self._state_directory = state_directory.resolve()
        self._clock = clock or _utc_now
        _require_owner_only_directory(self._state_directory)
        master_key = read_owner_only_file(master_key_file)
        if len(master_key) != _MASTER_KEY_BYTES:
            raise ValueError("master key file must contain exactly 32 bytes")
        self._cipher = AESGCM(master_key)
        try:
            self._ca_certificate = x509.load_pem_x509_certificate(
                read_owner_only_file(ca_certificate_file)
            )
            loaded_private_key = serialization.load_pem_private_key(
                read_owner_only_file(ca_private_key_file), password=None
            )
        except ValueError as exc:
            raise ValueError("TLS CA files are invalid") from exc
        if not isinstance(
            loaded_private_key,
            (
                dsa.DSAPrivateKey,
                ec.EllipticCurvePrivateKey,
                ed25519.Ed25519PrivateKey,
                ed448.Ed448PrivateKey,
                rsa.RSAPrivateKey,
            ),
        ):
            raise ValueError("TLS CA private key type cannot sign certificates")
        self._ca_private_key: CertificateIssuerPrivateKeyTypes = loaded_private_key
        if not _private_key_matches_certificate(
            self._ca_private_key, self._ca_certificate
        ):
            raise ValueError("TLS CA certificate and private key do not match")
        try:
            constraints = self._ca_certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except x509.ExtensionNotFound as exc:
            raise ValueError(
                "TLS CA certificate is not a certificate authority"
            ) from exc
        now = self._clock().astimezone(UTC)
        if (
            not constraints.ca
            or now < self._ca_certificate.not_valid_before_utc
            or now >= self._ca_certificate.not_valid_after_utc
        ):
            raise ValueError("TLS CA certificate is not a valid certificate authority")
        if (
            _SAFE_PREFIX_RE.fullmatch(app_prefix) is None
            or len(app_prefix) + _ALLOCATION_DIGEST_LENGTH + 1 > 63
        ):
            raise ValueError("app prefix is invalid")
        self._app_prefix = app_prefix

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str,
    ) -> FlyRuntimeSecrets:
        """Return the same encrypted-at-rest bundle until it is revoked."""
        identity = _validate_consumer_identity(consumer_identity)
        requester = _validate_identity(
            requester_identity,
            field="requester identity",
        )
        normalized_activation = activation_id.strip()
        digest = _activation_digest(normalized_activation)
        bundle_path, revoked_path = self._paths(digest)
        self._refuse_revoked(bundle_path, revoked_path)
        if bundle_path.exists():
            return self._load_active(
                bundle_path,
                revoked_path,
                digest,
                normalized_activation,
                requester,
                identity,
            )
        document = self._new_bundle(
            normalized_activation,
            digest,
            requester,
            identity,
        )
        encrypted = self._encrypt(document.encode(), _aad(digest))
        try:
            _exclusive_write(bundle_path, encrypted)
        except FileExistsError:
            return self._load_active(
                bundle_path,
                revoked_path,
                digest,
                normalized_activation,
                requester,
                identity,
            )
        self._refuse_revoked(bundle_path, revoked_path)
        return document.runtime()

    def revoke(self, activation_id: str) -> None:
        """Persist a content-free tombstone, then remove any encrypted bundle."""
        digest = _activation_digest(activation_id)
        bundle_path, revoked_path = self._paths(digest)
        with suppress(FileExistsError):
            _exclusive_write(revoked_path, b"revoked\n")
        with suppress(FileNotFoundError):
            bundle_path.unlink()

    def _paths(self, digest: str) -> tuple[Path, Path]:
        return (
            self._state_directory / f"{digest}.bundle",
            self._state_directory / f"{digest}.revoked",
        )

    @staticmethod
    def _refuse_revoked(bundle_path: Path, revoked_path: Path) -> None:
        """Make a durable tombstone win every issuance persistence race."""
        if not revoked_path.exists():
            return
        with suppress(FileNotFoundError):
            bundle_path.unlink()
        raise ValueError("runtime secrets for activation are revoked")

    def _load_active(
        self,
        bundle_path: Path,
        revoked_path: Path,
        digest: str,
        activation_id: str,
        requester_identity: str,
        consumer_identity: str,
    ) -> FlyRuntimeSecrets:
        """Load a bundle only while no revocation tombstone exists."""
        self._refuse_revoked(bundle_path, revoked_path)
        document = _load_document(self._cipher, bundle_path, digest)
        self._refuse_revoked(bundle_path, revoked_path)
        if (
            document.activation_id != activation_id
            or document.requester_identity != requester_identity
            or document.consumer_identity != consumer_identity
        ):
            raise ValueError("runtime secret state is invalid")
        return document.runtime()

    def _new_bundle(
        self,
        activation_id: str,
        digest: str,
        requester_identity: str,
        consumer_identity: str,
    ) -> _BundleDocument:
        credential = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        replay_state = (
            base64.urlsafe_b64encode(os.urandom(REPLAY_STATE_BYTES))
            .rstrip(b"=")
            .decode()
        )
        private_key = ec.generate_private_key(ec.SECP256R1())
        certificate = self._certificate(activation_id, digest, private_key)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return _BundleDocument(
            activation_id,
            requester_identity,
            consumer_identity,
            credential,
            replay_state,
            f"{consumer_identity}={credential}\n".encode(),
            certificate.public_bytes(serialization.Encoding.PEM),
            private_pem,
        )

    def _certificate(
        self,
        activation_id: str,
        digest: str,
        private_key: ec.EllipticCurvePrivateKey,
    ) -> x509.Certificate:
        now = self._clock().astimezone(UTC)
        hostname = f"*.vm.{self._app_prefix}-{digest}.internal"
        serial = (
            int.from_bytes(hashlib.sha256(activation_id.encode()).digest()[:19], "big")
            or 1
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
            )
            .issuer_name(self._ca_certificate.subject)
            .public_key(private_key.public_key())
            .serial_number(serial)
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(
                min(
                    now + _CERTIFICATE_VALIDITY,
                    self._ca_certificate.not_valid_after_utc,
                )
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
        )
        if isinstance(
            self._ca_private_key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)
        ):
            return builder.sign(self._ca_private_key, None)
        return builder.sign(self._ca_private_key, hashes.SHA256())

    def _encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext, aad)


class ReadOnlyFlySecretManager:
    """Read existing owner-bound bundles for routing; never issue or revoke."""

    def __init__(self, state_directory: Path, *, master_key_file: Path) -> None:
        """Bind existing encrypted state without loading CA signing authority."""
        self._state_directory = state_directory.resolve()
        _require_owner_only_directory(self._state_directory)
        self._cipher = _routing_cipher(master_key_file)

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str = "",
    ) -> FlyRuntimeSecrets:
        """Satisfy the driver protocol only with an already-issued active bundle."""
        try:
            digest = _activation_digest(activation_id)
            path = self._state_directory / f"{digest}.bundle"
            revoked = path.with_suffix(".revoked")
            if revoked.exists():
                raise ValueError("runtime secrets are revoked")
            document = _load_document(self._cipher, path, digest)
            if (
                revoked.exists()
                or document.activation_id != activation_id
                or document.requester_identity != requester_identity
                or document.consumer_identity != consumer_identity
            ):
                raise ValueError("runtime secret state is invalid")
        except ValueError:
            raise ProviderError(
                FailureReason.PROVIDER_REJECTED, retryable=False
            ) from None
        return document.runtime()

    def revoke(self, activation_id: str) -> None:
        """Refuse credential revocation outside the provisioning worker."""
        del activation_id
        raise ProviderError(FailureReason.PROVIDER_REJECTED, retryable=False)


def _routing_cipher(master_key_file: Path) -> AESGCM:
    """Load the bounded decryption key shared by read-only routing adapters."""
    master_key = read_owner_only_file(master_key_file)
    if len(master_key) != _MASTER_KEY_BYTES:
        raise ValueError("master key file must contain exactly 32 bytes")
    return AESGCM(master_key)


class EncryptedFileRoutingCredentialVerifier:
    """Verify issued routing credentials without holding issuance authority."""

    def __init__(self, state_directory: Path, *, master_key_file: Path) -> None:
        """Bind the encrypted bundle directory and its mounted decryption key."""
        self._state_directory = state_directory.resolve()
        _require_owner_only_directory(self._state_directory)
        self._cipher = _routing_cipher(master_key_file)

    async def verify_credential(self, credential: str) -> RoutingPrincipal | None:
        """Return the one exact owner bound to *credential*, or fail closed."""
        return await asyncio.to_thread(self._verify_credential, credential)

    def _verify_credential(self, credential: str) -> RoutingPrincipal | None:
        """Scan every active bundle so comparison timing does not select an owner."""
        try:
            presented = credential.encode()
        except UnicodeError:
            return None
        matched: RoutingPrincipal | None = None
        conflicting = False
        for bundle_path in sorted(self._state_directory.glob("*.bundle")):
            digest = bundle_path.stem
            if _BUNDLE_NAME_RE.fullmatch(digest) is None:
                return None
            revoked_path = bundle_path.with_suffix(".revoked")
            if revoked_path.exists():
                return None
            try:
                document = _load_document(self._cipher, bundle_path, digest)
            except ValueError:
                return None
            if revoked_path.exists():
                return None
            candidate = document.consumer_credential.encode()
            if hmac.compare_digest(candidate, presented):
                principal = RoutingPrincipal(
                    document.requester_identity,
                    document.consumer_identity,
                    document.replay_state,
                )
                conflicting = conflicting or (
                    matched is not None and matched != principal
                )
                matched = principal
        return None if conflicting else matched


def _private_key_matches_certificate(
    private_key: CertificateIssuerPrivateKeyTypes,
    certificate: x509.Certificate,
) -> bool:
    """Compare public encodings without exposing either key in diagnostics."""
    private_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    certificate_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_public == certificate_public


def _require_owner_only_directory(path: Path) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("secret state directory is unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("secret state directory must be owner-only")


def _exclusive_write(path: Path, value: bytes) -> None:
    """Create one owner-only file atomically and durably."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load_document(cipher: AESGCM, path: Path, digest: str) -> _BundleDocument:
    """Authenticate and decode one encrypted bundle without exposing plaintext."""
    encrypted = read_owner_only_file(path)
    if len(encrypted) <= _NONCE_BYTES:
        raise ValueError("runtime secret state is invalid")
    nonce, ciphertext = encrypted[:_NONCE_BYTES], encrypted[_NONCE_BYTES:]
    try:
        plaintext = cipher.decrypt(nonce, ciphertext, _aad(digest))
    except (InvalidTag, ValueError):
        raise ValueError("runtime secret state is invalid") from None
    return _BundleDocument.decode(plaintext)


def _validate_consumer_identity(value: str) -> str:
    return _validate_identity(value, field="consumer identity")


def _validate_identity(value: str, *, field: str) -> str:
    normalized = value.strip()
    if _SAFE_IDENTITY_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field} is invalid for the registry")
    return normalized


def _activation_digest(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("activation id must not be blank")
    return hashlib.sha256(normalized.encode()).hexdigest()[:_ALLOCATION_DIGEST_LENGTH]


def _aad(activation_digest: str) -> bytes:
    return f"creek-runtime-v2\0{activation_digest}".encode()


def _legacy_replay_state(activation_id: str, credential: str) -> str:
    """Derive stable opaque replay state for a pre-replay encrypted bundle."""
    digest = hmac.digest(
        credential.encode(),
        b"creek-fly-replay-v1\0" + activation_id.encode(),
        "sha384",
    )
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("runtime secret field is invalid")
    return base64.b64decode(value, validate=True)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
