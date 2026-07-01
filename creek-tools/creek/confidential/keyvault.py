"""At-rest volume key: user-held passphrase + independent recovery key (#758).

The vault's durable volume is encrypted with a **volume master key (VMK)** the
operator never stores in plaintext. The VMK is wrapped twice, giving two
independent unwrap paths:

- **passphrase path** — a key-encryption key (KEK) is derived from the user's
  passphrase with **Argon2id** (memory-hard, sane params) over a random salt,
  and used to AES-256-GCM-wrap the VMK.
- **recovery path** — at setup a one-time, high-entropy **recovery key** is
  generated and shown to the user to store. Its KEK is derived with HKDF-SHA256
  (the recovery key is already full-entropy, so a memory-hard KDF buys nothing)
  and used to wrap the VMK a second time.

Security properties this module guarantees:

- **No operator escrow, no reset.** The persisted :class:`KeyVault` holds only
  ciphertext + public parameters (salt, Argon2id cost, AEAD nonces). The VMK,
  the passphrase, and the recovery key are never persisted. Losing *both* the
  passphrase and the recovery key is unrecoverable — by design, mirroring
  Obsidian Sync's optional end-to-end encryption.
- **Integrity.** AES-GCM authenticates each wrapped copy, so a wrong passphrase /
  recovery key, or a tampered artifact, fails closed with :class:`UnlockError`
  rather than returning garbage.
- **No secret logging.** This module never logs; secret material lives only in
  local variables and the returned ``bytes`` / one-time recovery string.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VMK_BYTES: Final[int] = 32
"""Volume master key size — a 256-bit AES key."""

_SALT_BYTES: Final[int] = 16
_NONCE_BYTES: Final[int] = 12
_RECOVERY_BYTES: Final[int] = 32
"""Recovery-key entropy — 256 bits of ``os.urandom``."""

# Argon2id cost parameters. Comfortably above the OWASP minimum (memory 19 MiB,
# t=2, p=1); memory-hardness is the primary defence against offline passphrase
# cracking. Persisted per-vault so a future increase does not lock out old vaults.
_ARGON2_TIME_COST: Final[int] = 3
_ARGON2_LANES: Final[int] = 4
_ARGON2_MEMORY_KIB: Final[int] = 64 * 1024  # 64 MiB

# AEAD associated-data domain separators — bind each wrapped copy to its purpose
# so a ciphertext can never be swapped between the two unwrap paths.
_AAD_PASSPHRASE: Final[bytes] = b"creek.confidential.vmk.passphrase.v1"
_AAD_RECOVERY: Final[bytes] = b"creek.confidential.vmk.recovery.v1"
_HKDF_INFO: Final[bytes] = b"creek.confidential.recovery-kek.v1"

_VAULT_VERSION: Final[int] = 1


class UnlockError(Exception):
    """A wrong passphrase / recovery key, or a tampered vault, prevents unwrap.

    Carries a single fixed message and is raised argument-free at every call
    site, so it deliberately does not distinguish "wrong key" from "malformed
    input" from "corrupt ciphertext" — it leaks nothing about *why* an unwrap
    failed.
    """

    _MESSAGE = "unable to unlock the volume key"

    def __init__(self) -> None:
        """Initialise with the fixed, non-revealing message."""
        super().__init__(self._MESSAGE)


@dataclass(frozen=True)
class _Wrapped:
    """One AES-GCM-wrapped copy of the VMK (nonce + ciphertext, both public)."""

    nonce: bytes
    ciphertext: bytes

    def to_dict(self) -> dict[str, str]:
        """Serialise to hex strings (ciphertext-only, safe to persist)."""
        return {"nonce": self.nonce.hex(), "ciphertext": self.ciphertext.hex()}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> _Wrapped:
        """Rebuild from the hex-encoded persisted form."""
        return cls(
            nonce=bytes.fromhex(data["nonce"]),
            ciphertext=bytes.fromhex(data["ciphertext"]),
        )


@dataclass(frozen=True)
class KeyVault:
    """The persisted, **ciphertext-only** key vault (no plaintext key material).

    Attributes:
        version: Schema version.
        salt: Argon2id salt (public).
        time_cost / lanes / memory_kib: Argon2id parameters used for the
            passphrase KEK (persisted so unlock reproduces them).
        passphrase_wrapped: VMK wrapped under the passphrase-derived KEK.
        recovery_wrapped: VMK wrapped under the recovery-key-derived KEK.
    """

    version: int
    salt: bytes
    time_cost: int
    lanes: int
    memory_kib: int
    passphrase_wrapped: _Wrapped
    recovery_wrapped: _Wrapped

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable, ciphertext-only representation."""
        return {
            "version": self.version,
            "kdf": {
                "algorithm": "argon2id",
                "salt": self.salt.hex(),
                "time_cost": self.time_cost,
                "lanes": self.lanes,
                "memory_kib": self.memory_kib,
            },
            "passphrase_wrapped": self.passphrase_wrapped.to_dict(),
            "recovery_wrapped": self.recovery_wrapped.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyVault:
        """Rebuild a :class:`KeyVault` from its persisted form."""
        kdf = data["kdf"]
        return cls(
            version=int(data["version"]),
            salt=bytes.fromhex(kdf["salt"]),
            time_cost=int(kdf["time_cost"]),
            lanes=int(kdf["lanes"]),
            memory_kib=int(kdf["memory_kib"]),
            passphrase_wrapped=_Wrapped.from_dict(data["passphrase_wrapped"]),
            recovery_wrapped=_Wrapped.from_dict(data["recovery_wrapped"]),
        )


@dataclass(frozen=True)
class SetupResult:
    """The outcome of :func:`create_key_vault`.

    Attributes:
        vault: The ciphertext-only :class:`KeyVault` to persist.
        recovery_key: The one-time recovery key to **show the user once** and
            never persist operator-side. It is the only way (besides the
            passphrase) to unwrap the volume.
    """

    vault: KeyVault
    recovery_key: str


def _derive_passphrase_kek(
    passphrase: str, salt: bytes, *, time_cost: int, lanes: int, memory_kib: int
) -> bytes:
    """Derive the passphrase KEK via Argon2id over *salt*."""
    return Argon2id(
        salt=salt,
        length=_VMK_BYTES,
        iterations=time_cost,
        lanes=lanes,
        memory_cost=memory_kib,
    ).derive(passphrase.encode("utf-8"))


def _derive_recovery_kek(recovery_bytes: bytes) -> bytes:
    """Derive the recovery KEK from the full-entropy recovery key via HKDF."""
    return HKDF(
        algorithm=hashes.SHA256(), length=_VMK_BYTES, salt=None, info=_HKDF_INFO
    ).derive(recovery_bytes)


def _wrap(kek: bytes, vmk: bytes, aad: bytes) -> _Wrapped:
    """AES-256-GCM-wrap *vmk* under *kek*, bound to *aad*."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(kek).encrypt(nonce, vmk, aad)
    return _Wrapped(nonce=nonce, ciphertext=ciphertext)


def _unwrap(kek: bytes, wrapped: _Wrapped, aad: bytes) -> bytes:
    """Unwrap the VMK, raising :class:`UnlockError` on any authentication failure."""
    try:
        return AESGCM(kek).decrypt(wrapped.nonce, wrapped.ciphertext, aad)
    except InvalidTag as exc:
        raise UnlockError from exc


def _encode_recovery(recovery_bytes: bytes) -> str:
    """Encode the recovery key as grouped, unpadded, uppercase base32."""
    raw = base64.b32encode(recovery_bytes).decode("ascii").rstrip("=")
    return "-".join(raw[i : i + 5] for i in range(0, len(raw), 5))


def _decode_recovery(recovery_key: str) -> bytes:
    """Decode a (possibly grouped/lowercase) recovery key back to bytes.

    Raises :class:`UnlockError` on a malformed key rather than a raw
    ``binascii``/``ValueError`` so callers handle one failure type.
    """
    compact = recovery_key.replace("-", "").replace(" ", "").strip().upper()
    padding = "=" * (-len(compact) % 8)
    try:
        return base64.b32decode(compact + padding)
    except (ValueError, TypeError) as exc:
        raise UnlockError from exc


def create_key_vault(passphrase: str) -> SetupResult:
    """Create a fresh key vault for *passphrase*, returning the one-time recovery key.

    Generates a random VMK and wraps it under both a passphrase-derived KEK and a
    freshly generated recovery key. The plaintext VMK is discarded when this
    returns; only the ciphertext-only :class:`KeyVault` should be persisted.

    Args:
        passphrase: The user's passphrase. Must be non-empty.

    Returns:
        A :class:`SetupResult` carrying the vault to persist and the recovery key
        to surface to the user exactly once.

    Raises:
        ValueError: If *passphrase* is empty.
    """
    if not passphrase:
        msg = "passphrase must not be empty"
        raise ValueError(msg)

    vmk = os.urandom(_VMK_BYTES)
    salt = os.urandom(_SALT_BYTES)
    passphrase_kek = _derive_passphrase_kek(
        passphrase,
        salt,
        time_cost=_ARGON2_TIME_COST,
        lanes=_ARGON2_LANES,
        memory_kib=_ARGON2_MEMORY_KIB,
    )
    passphrase_wrapped = _wrap(passphrase_kek, vmk, _AAD_PASSPHRASE)

    recovery_bytes = os.urandom(_RECOVERY_BYTES)
    recovery_wrapped = _wrap(_derive_recovery_kek(recovery_bytes), vmk, _AAD_RECOVERY)

    vault = KeyVault(
        version=_VAULT_VERSION,
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        lanes=_ARGON2_LANES,
        memory_kib=_ARGON2_MEMORY_KIB,
        passphrase_wrapped=passphrase_wrapped,
        recovery_wrapped=recovery_wrapped,
    )
    return SetupResult(vault=vault, recovery_key=_encode_recovery(recovery_bytes))


def unlock_with_passphrase(vault: KeyVault, passphrase: str) -> bytes:
    """Return the VMK by unwrapping the passphrase copy (raises ``UnlockError``)."""
    kek = _derive_passphrase_kek(
        passphrase,
        vault.salt,
        time_cost=vault.time_cost,
        lanes=vault.lanes,
        memory_kib=vault.memory_kib,
    )
    return _unwrap(kek, vault.passphrase_wrapped, _AAD_PASSPHRASE)


def unlock_with_recovery(vault: KeyVault, recovery_key: str) -> bytes:
    """Return the VMK by unwrapping the recovery copy, or raise :class:`UnlockError`."""
    kek = _derive_recovery_kek(_decode_recovery(recovery_key))
    return _unwrap(kek, vault.recovery_wrapped, _AAD_RECOVERY)


def save_key_vault(vault: KeyVault, path: Path) -> None:
    """Persist the ciphertext-only vault to *path* atomically, owner-only.

    Written to a temp file in the same directory (created ``0600`` from the
    start — never widen-then-narrow), fsync'd, then ``os.replace``-d into place.
    This matters more than usual here: the artifact is the *only* copy, and the
    "no reset" threat model means a torn write (crash / disk-full / power loss)
    would turn a recoverable state into accidental permanent lockout with no user
    error. The recovery key is NOT part of the vault and is never written here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(vault.to_dict(), indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:  # mkstemp already created it 0600
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_key_vault(path: Path) -> KeyVault:
    """Load a persisted :class:`KeyVault` from *path*."""
    return KeyVault.from_dict(json.loads(path.read_text(encoding="utf-8")))
