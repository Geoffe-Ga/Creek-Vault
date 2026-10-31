"""The attesting vendor's loopback sidecar on a managed vault Machine (#1896).

ADR-0016 O5 with Privatemode (owner decision 2026-10-21 on #1896) runs the
vendor's attesting proxy as a **loopback sidecar** beside the vault. This
module owns both halves of that:

- **The pin, as fleet policy.** :class:`PoolSidecarPolicy` carries the
  operator-filled values — the vendor image reference (digest form only), the
  SHA-256 of the proxy binary the bootstrap will execute, the loopback port,
  the single egress destination and the attestation pin — and
  :meth:`PoolSidecarPolicy.runtime_env` renders them into the Machine
  ``env``. ``_verify_machine`` compares that ``env`` exactly, so a Machine
  without the sidecar, or with any other pin, is not admitted, and the
  converge tool (#1869) treats a drifted sidecar pin like any other
  policy-owned field.
- **The launch.** :func:`start_pool_sidecar` runs inside the root replay
  bootstrap, before the privilege drop. It refuses to start a binary whose
  digest is not the pinned one, hands the vendor key to the sidecar process
  only (the key file is made root-owned ``0400``, so the vault's own user can
  never read it), discards the sidecar's output (Creek cannot vouch that a
  vendor log line is content-free), and never stops the vault: a sidecar that
  cannot start leaves the pool failing closed, not the vault down.

**Vendor-interface assumptions, stated so nobody mistakes them for checks.**
Creek could not reach ``docs.privatemode.ai`` when this was written. Three
facts about the pinned proxy are therefore the operator's to verify against
that image before :class:`PoolSidecarPolicy` is set anywhere, each recorded in
``docs/pool-measurement-pin.md``: the listen-port flag
(:data:`SIDECAR_PORT_FLAG`), the environment variable the proxy reads its key
from (:data:`SIDECAR_KEY_ENV`), and that it binds loopback only. Each wrong
guess fails closed — a proxy that cannot start, or one with no key that the
vendor answers 401 — except the last, which is why it is a precondition and
not a default.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek.classify.llm.pool_admission import DEFAULT_POOL_RATE_PER_HOUR, POOL_RATE_ENV
from creek.classify.llm.pool_attestation import (
    MAX_PINNED_MEASUREMENTS,
    POOL_EGRESS_ENV,
    POOL_MEASUREMENTS_ENV,
    POOL_MIN_VERSION_ENV,
    POOL_SIGNING_IDENTITY_ENV,
    valid_egress,
)
from creek.classify.llm.pool_boundary import POOL_SIDECAR_VENDOR, POOL_VENDOR_ENV
from creek_mcp.provisioning.pool_bundle import POOL_VENDOR_KEY_GUEST_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

POOL_SIDECAR_IMAGE_ENV: Final[str] = "CREEK_POOL_SIDECAR_IMAGE"
"""The vendor image the sidecar binary was taken from, digest form."""

POOL_SIDECAR_SHA256_ENV: Final[str] = "CREEK_POOL_SIDECAR_SHA256"
"""SHA-256 of the proxy binary the bootstrap is allowed to execute."""

POOL_SIDECAR_PORT_ENV: Final[str] = "CREEK_POOL_SIDECAR_PORT"
"""The loopback port the sidecar listens on."""

SIDECAR_BINARY: Final[Path] = Path("/opt/creek-pool-sidecar/privatemode-proxy")
"""Where the vault image carries the pinned proxy binary."""

SIDECAR_UID: Final[int] = 10002
"""The sidecar's own user: neither root nor the vault's ``10001``."""

SIDECAR_GID: Final[int] = 10002
"""The sidecar's own group."""

SIDECAR_PORT_FLAG: Final[str] = "--port"
"""The proxy's listen-port flag. **Vendor assumption; operator-verified.**"""

SIDECAR_KEY_ENV: Final[str] = "PRIVATEMODE_API_KEY"
"""Variable the proxy reads its key from. **Vendor assumption; operator-verified.**

An environment variable, never an argument: ``/proc/<pid>/cmdline`` is
readable by every user on the Machine, ``/proc/<pid>/environ`` only by the
process's own user and root.
"""

DEFAULT_SIDECAR_PORT: Final[int] = 8085
"""Default loopback port; distinct from the vault's own 8823."""

_IMMUTABLE_IMAGE: Final[re.Pattern[str]] = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_MAX_PORT: Final[int] = 65_535
_MAX_KEY_BYTES: Final[int] = 16 * 1024
_READ_CHUNK: Final[int] = 1 << 20
_PRIVILEGED_PORTS: Final[int] = 1024


def _valid_measurements(measurements: tuple[str, ...]) -> bool:
    """Return whether *measurements* is one or two lowercase SHA-256 digests."""
    return 1 <= len(measurements) <= MAX_PINNED_MEASUREMENTS and all(
        _SHA256_HEX.fullmatch(entry) for entry in measurements
    )


@dataclass(frozen=True, slots=True)
class PoolSidecarPolicy:
    """The operator-filled sidecar pin, as fleet policy.

    Attributes:
        image: The vendor image reference, ``<name>@sha256:<64 hex>`` only.
        binary_sha256: SHA-256 of the proxy binary inside the vault image.
        egress: The sidecar's single egress destination, ``host:port``.
        signing_identity: The vendor's build signing identity.
        min_version: The lowest admitted attested runtime version.
        measurements: One pinned measurement digest, or two in a re-pin window.
        port: The loopback port the sidecar listens on.
        rate_per_hour: The vault's hourly pool admission rate.
    """

    image: str
    binary_sha256: str
    egress: str
    signing_identity: str
    min_version: str
    measurements: tuple[str, ...]
    port: int = DEFAULT_SIDECAR_PORT
    rate_per_hour: int = DEFAULT_POOL_RATE_PER_HOUR

    def __post_init__(self) -> None:
        """Refuse a pin that is floating, partial or malformed."""
        if _IMMUTABLE_IMAGE.fullmatch(self.image) is None:
            raise ValueError("pool sidecar image must use an immutable sha256 digest")
        if _SHA256_HEX.fullmatch(self.binary_sha256) is None:
            raise ValueError("pool sidecar binary digest must be lowercase SHA-256")
        if not valid_egress(self.egress):
            raise ValueError("pool sidecar egress must be one host:port")
        if not self.signing_identity.strip() or not self.min_version.strip():
            raise ValueError("pool sidecar attestation pin must be complete")
        if not _valid_measurements(self.measurements):
            raise ValueError("pool sidecar measurements must be one or two SHA-256")
        if not _PRIVILEGED_PORTS < self.port <= _MAX_PORT:
            raise ValueError("pool sidecar port must be unprivileged")
        if self.rate_per_hour <= 0:
            raise ValueError("pool admission rate must be positive")

    def runtime_env(self) -> dict[str, str]:
        """Return the Machine ``env`` entries this pin contributes.

        Returns:
            Every sidecar and attestation-pin key; no secret value.
        """
        return {
            POOL_VENDOR_ENV: POOL_SIDECAR_VENDOR,
            POOL_SIDECAR_IMAGE_ENV: self.image,
            POOL_SIDECAR_SHA256_ENV: self.binary_sha256,
            POOL_SIDECAR_PORT_ENV: str(self.port),
            POOL_EGRESS_ENV: self.egress,
            POOL_SIGNING_IDENTITY_ENV: self.signing_identity,
            POOL_MIN_VERSION_ENV: self.min_version,
            POOL_MEASUREMENTS_ENV: ",".join(self.measurements),
            POOL_RATE_ENV: str(self.rate_per_hour),
        }


def sidecar_declared(environ: Mapping[str, str]) -> bool:
    """Return whether the Machine declares the attesting sidecar.

    Args:
        environ: The Machine environment.

    Returns:
        ``True`` when :data:`POOL_VENDOR_ENV` names the sidecar vendor.
    """
    return environ.get(POOL_VENDOR_ENV, "").strip().lower() == POOL_SIDECAR_VENDOR


def _file_sha256(path: Path) -> str | None:
    """Return *path*'s SHA-256, or ``None`` when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _read_key(path: Path) -> str | None:
    """Return the vendor key from its root-owned file, or ``None``."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _MAX_KEY_BYTES:
        return None
    try:
        return raw.decode("utf-8").strip() or None
    except UnicodeError:
        return None


def _refuse(reason: str) -> bool:
    """Record a content-free reason the sidecar did not start; return ``False``."""
    print(f"creek-fly-vault: pool sidecar not started: {reason}", file=sys.stderr)
    return False


def sidecar_argv(environ: Mapping[str, str], binary: Path) -> list[str] | None:
    """Return the sidecar's argument vector, or ``None`` on a bad port.

    Args:
        environ: The Machine environment.
        binary: The pinned binary.

    Returns:
        The vector; it never carries the key.
    """
    port = environ.get(POOL_SIDECAR_PORT_ENV, "").strip()
    if not port.isdigit() or not _PRIVILEGED_PORTS < int(port) <= _MAX_PORT:
        return None
    return [str(binary), SIDECAR_PORT_FLAG, port]


def start_pool_sidecar(
    environ: Mapping[str, str],
    *,
    binary: Path = SIDECAR_BINARY,
    key_file: Path = Path(POOL_VENDOR_KEY_GUEST_PATH),
    spawn: Callable[..., object] = subprocess.Popen,
) -> bool:
    """Start the pinned sidecar as its own user, or record why not.

    Must run as root, before the vault drops privileges: it reads the
    root-owned key file. Every refusal is a closed, content-free reason on
    stderr and returns ``False``; none raises, because a missing sidecar
    must leave the pool failing closed rather than the vault down.

    Args:
        environ: The Machine environment.
        binary: The proxy binary to execute.
        key_file: The root-owned vendor key file.
        spawn: Process launcher, injectable for tests.

    Returns:
        ``True`` when the sidecar was launched.
    """
    if not sidecar_declared(environ):
        return False
    pinned = environ.get(POOL_SIDECAR_SHA256_ENV, "").strip().lower()
    if _SHA256_HEX.fullmatch(pinned) is None or _file_sha256(binary) != pinned:
        return _refuse("binary_unpinned")
    argv = sidecar_argv(environ, binary)
    if argv is None:
        return _refuse("port_invalid")
    key = _read_key(key_file)
    if key is None:
        return _refuse("key_unavailable")
    try:
        spawn(
            argv,
            env={SIDECAR_KEY_ENV: key},
            user=SIDECAR_UID,
            group=SIDECAR_GID,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _refuse("spawn_failed")
    return True


def key_file_owner(environ: Mapping[str, str]) -> tuple[int, int] | None:
    """Return who must own the vendor key file, or ``None`` for the default.

    Args:
        environ: The Machine environment.

    Returns:
        ``(0, 0)`` in sidecar mode — root only, so the vault's user cannot
        read the key and only the bootstrap hands it to the sidecar — and
        ``None`` otherwise.
    """
    return (0, 0) if sidecar_declared(environ) else None
