"""The vault-side attestation gate in front of the loopback sidecar (#1896).

Under ADR-0016 O5 with Privatemode (owner decision 2026-10-21 on #1896), the
vendor's attesting client runs as a **loopback sidecar** beside the vault: the
vendor's proxy performs the remote attestation of the backend and holds the
vendor key. This module is the vault's half of that arrangement. Before any
content dial it reads the sidecar's attestation report over loopback and
compares it with the **pin** the Machine carries; on any disagreement it raises
:class:`PoolAttestationError` and nothing is sent (ADR-0016 T18).

What this module does, and does not, establish — stated plainly because the
owner's waiver of Decision 3.3 condition (f) requires every dependent artifact
to say so:

- **Tested here:** given a report, the vault refuses unless the report says the
  attestation verified, over an attested channel, under the pinned build
  signing identity, for a measurement in the pinned set, with a
  transparency-log entry, at or above the pinned minimum version, through the
  pinned single egress destination. A missing, unreachable or malformed report
  also refuses. Each refusal happens before any content leaves the process.
- **Vendor-asserted, not verified by Creek:** that the report is *true* — the
  cryptographic verification of the hardware quote, the measurement, the
  Sigstore signature and the transparency-log inclusion happens inside the
  vendor's proxy, and the vendor's binding of the attested measurement to the
  live session key (the substitute for D05 condition (c)) is the vendor's
  design. The attested image has not been rebuilt from source by Creek or
  anyone independent; condition (f) is **waived, vendor-asserted**.

The report document is **Creek's contract**, served at
:data:`~creek.classify.llm.pool_boundary.POOL_ATTESTATION_PATH`. Whether the
pinned vendor proxy serves it natively or through a thin adapter is an
operator verification recorded in ``docs/pool-measurement-pin.md``; until a
sidecar answers it, the vault refuses every pool request rather than trusting
an unreported state.

The pin is read from the Machine environment only — never from vault config —
for the same reason :data:`~creek.classify.llm.pool_boundary.POOL_URL_ENV` is.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from creek.classify.llm.pool_boundary import (
    PoolBoundaryError,
    pool_attestation_report,
    pool_response_ok,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

POOL_SIGNING_IDENTITY_ENV: Final[str] = "CREEK_POOL_SIGNING_IDENTITY"
"""The vendor's build signing identity measurements must be published under."""

POOL_MIN_VERSION_ENV: Final[str] = "CREEK_POOL_MIN_VERSION"
"""The lowest attested runtime version admitted (Decision 3.2)."""

POOL_MEASUREMENTS_ENV: Final[str] = "CREEK_POOL_MEASUREMENTS"
"""Comma-separated SHA-256 measurement digests admitted right now.

One entry normally; two during Decision 3.2's review-gated re-pin window, when
the old and the new measurement are both served. Never more: a third would be
a window nobody is closing.
"""

POOL_EGRESS_ENV: Final[str] = "CREEK_POOL_EGRESS"
"""The sidecar's single egress destination, ``host:port`` (ADR-0016 D1.2)."""

ATTESTED_CHANNEL: Final[str] = "attested_tls"
"""The only channel value a report may carry and be admitted."""

VERIFIED_STATUS: Final[str] = "verified"
"""The only attestation status a report may carry and be admitted."""

MAX_PINNED_MEASUREMENTS: Final[int] = 2
"""The pinned set's ceiling: the current measurement plus one under review."""

_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_VERSION: Final[re.Pattern[str]] = re.compile(r"v?(\d+(?:\.\d+){0,3})")
_EGRESS: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+:\d{1,5}"
)
_MAX_PORT: Final[int] = 65_535


@unique
class AttestationVerdict(StrEnum):
    """The closed set of outcomes of one attestation check.

    Every value is content-free: it names which comparison failed, never what
    the sidecar reported.

    Attributes:
        VERIFIED: The report matches the pin; content may be sent.
        UNPINNED: The Machine carries no complete pin, so nothing can match.
        UNREACHABLE: The sidecar did not answer its report usably.
        MALFORMED: The report is not the contract's shape.
        NOT_VERIFIED: The sidecar reports its own attestation did not pass.
        PLAIN_CHANNEL: The upstream channel is not attestation-bound TLS.
        FOREIGN_IDENTITY: The measurement is published under another identity.
        MEASUREMENT_UNPINNED: The measurement is not in the pinned set — a
            tampered measurement and a merely unknown one land here alike.
        NO_TRANSPARENCY_LOG: The report names no transparency-log entry.
        VERSION_BELOW_MINIMUM: The attested runtime predates the pin's floor.
        EGRESS_MISMATCH: The sidecar dials somewhere other than the pin.
    """

    VERIFIED = "verified"
    UNPINNED = "unpinned"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"
    NOT_VERIFIED = "not_verified"
    PLAIN_CHANNEL = "plain_channel"
    FOREIGN_IDENTITY = "foreign_identity"
    MEASUREMENT_UNPINNED = "measurement_unpinned"
    NO_TRANSPARENCY_LOG = "no_transparency_log"
    VERSION_BELOW_MINIMUM = "version_below_minimum"
    EGRESS_MISMATCH = "egress_mismatch"


class PoolAttestationError(PoolBoundaryError):
    """The sidecar's attestation does not match the pin, so nothing is sent.

    A :class:`~creek.classify.llm.pool_boundary.PoolBoundaryError`, so every
    generation call site already degrades it to the route's existing refusal
    (ADR-0016 Decision 1 item 10): a refusal, never a fallback.

    Attributes:
        verdict: Which comparison failed; content-free.
    """

    def __init__(self, verdict: AttestationVerdict) -> None:
        """Store the verdict behind a fixed, content-free message.

        Args:
            verdict: The failing verdict.
        """
        super().__init__(f"the attesting sidecar is not admitted: {verdict.value}")
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class PoolPin:
    """What the Machine admits from the sidecar (ADR-0016 Decision 3.2).

    Attributes:
        signing_identity: The vendor's build signing identity.
        min_version: The lowest admitted runtime version, as integers.
        measurements: The admitted measurement digests, lowercase hex.
        egress: The sidecar's single egress destination, ``host:port``.
    """

    signing_identity: str
    min_version: tuple[int, ...]
    measurements: frozenset[str]
    egress: str


def _version(value: object) -> tuple[int, ...] | None:
    """Return *value* as a comparable version tuple, or ``None``.

    Args:
        value: A dotted numeric version, optionally ``v``-prefixed.

    Returns:
        The integer parts, or ``None`` for anything else.
    """
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _measurements(raw: str) -> frozenset[str] | None:
    """Return the pinned measurement set, or ``None`` when it is unusable.

    Args:
        raw: The comma-separated environment value.

    Returns:
        One or two lowercase SHA-256 hex digests, or ``None``.
    """
    entries = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    if not 1 <= len(entries) <= MAX_PINNED_MEASUREMENTS:
        return None
    if not all(_SHA256_HEX.fullmatch(entry) for entry in entries):
        return None
    return frozenset(entries)


def valid_egress(value: str) -> bool:
    """Return whether *value* is a pinned egress destination, ``host:port``.

    A lowercase DNS name with at least one dot and an explicit port; no
    scheme, path, credentials or IP literal, so the pin names exactly one
    vendor host. Lowercase is the canonical form: callers holding operator
    input lowercase it first (:func:`load_pool_pin`,
    ``PoolSidecarPolicy``), so the pin compares equal however it was typed.
    A final label of digits alone is a hostname, not an address (#2042);
    the regex already refuses every IPv6 literal, and
    :func:`_is_address_literal` refuses the IPv4 ones.

    Args:
        value: The candidate destination.

    Returns:
        ``True`` when it has that shape and the port is in range.
    """
    if _EGRESS.fullmatch(value) is None:
        return False
    host, port = value.rsplit(":", 1)
    if _is_address_literal(host):
        return False
    return 0 < int(port) <= _MAX_PORT


def _is_address_literal(host: str) -> bool:
    """Return whether *host* is an IP address rather than a name.

    :func:`ipaddress.ip_address` recognises the canonical forms only.
    :func:`socket.inet_aton` also accepts the legacy shorthand the system
    resolver still reads as an address (``127.1``, ``0x7f.1``), which the
    canonical parser would let through as a "hostname".

    Args:
        host: The host part of an egress pin.

    Returns:
        ``True`` when either parser reads *host* as an address.
    """
    return _parses_as_canonical_address(host) or _parses_as_legacy_ipv4(host)


def _parses_as_canonical_address(host: str) -> bool:
    """Return whether :func:`ipaddress.ip_address` reads *host* as an address."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _parses_as_legacy_ipv4(host: str) -> bool:
    """Return whether :func:`socket.inet_aton` reads *host* as an IPv4 address."""
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


def load_pool_pin(environ: Mapping[str, str] | None = None) -> PoolPin | None:
    """Return the Machine's pin, or ``None`` when any part is absent or invalid.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The complete pin, or ``None``. A partial pin is no pin: every
        comparison must have something to compare against.
    """
    source = os.environ if environ is None else environ
    identity = source.get(POOL_SIGNING_IDENTITY_ENV, "").strip()
    floor = _version(source.get(POOL_MIN_VERSION_ENV, ""))
    measurements = _measurements(source.get(POOL_MEASUREMENTS_ENV, ""))
    egress = source.get(POOL_EGRESS_ENV, "").strip().lower()
    if not identity or floor is None or measurements is None:
        return None
    if not valid_egress(egress):
        return None
    return PoolPin(identity, floor, measurements, egress)


def _text(report: Mapping[str, object], key: str) -> str | None:
    """Return the string at *key*, or ``None`` when absent or not a string."""
    value = report.get(key)
    return value if isinstance(value, str) else None


def _measurement_pinned(measurement: str | None, pinned: frozenset[str]) -> bool:
    """Return whether *measurement* is in *pinned*, compared in constant time."""
    if measurement is None:
        return False
    candidate = measurement.strip().lower()
    return any(hmac.compare_digest(candidate, entry) for entry in sorted(pinned))


def judge_report(report: object, pin: PoolPin | None) -> AttestationVerdict:
    """Return the verdict for one attestation report against *pin*.

    The checks run in a fixed order and the first failure wins, so a report
    failing two checks is always named the same way.

    Args:
        report: The decoded report body.
        pin: The Machine's pin, or ``None`` when it carries none.

    Returns:
        :attr:`AttestationVerdict.VERIFIED` only when every check passes.
    """
    if pin is None:
        return AttestationVerdict.UNPINNED
    if not isinstance(report, dict):
        return AttestationVerdict.MALFORMED
    version = _version(report.get("version"))
    checks: tuple[tuple[bool, AttestationVerdict], ...] = (
        (_text(report, "status") == VERIFIED_STATUS, AttestationVerdict.NOT_VERIFIED),
        (
            _text(report, "channel") == ATTESTED_CHANNEL,
            AttestationVerdict.PLAIN_CHANNEL,
        ),
        (
            _text(report, "signing_identity") == pin.signing_identity,
            AttestationVerdict.FOREIGN_IDENTITY,
        ),
        (
            _measurement_pinned(_text(report, "measurement"), pin.measurements),
            AttestationVerdict.MEASUREMENT_UNPINNED,
        ),
        (
            bool((_text(report, "transparency_log_entry") or "").strip()),
            AttestationVerdict.NO_TRANSPARENCY_LOG,
        ),
        (version is not None, AttestationVerdict.MALFORMED),
        (
            version is not None and version >= pin.min_version,
            AttestationVerdict.VERSION_BELOW_MINIMUM,
        ),
        (
            (_text(report, "egress") or "").strip().lower() == pin.egress,
            AttestationVerdict.EGRESS_MISMATCH,
        ),
    )
    for passed, verdict in checks:
        if not passed:
            return verdict
    return AttestationVerdict.VERIFIED


def attested_report(
    *, budget: float | None = None, environ: Mapping[str, str] | None = None
) -> Mapping[str, object]:
    """Return the sidecar's report when it matches the pin; refuse otherwise.

    The pin is checked first, so a Machine without one never dials at all.

    Args:
        budget: Seconds left in the vault request.
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The verified report, which also carries the pool's identity listing
        (``runtime`` and ``models``) for the manifest comparison.

    Raises:
        PoolAttestationError: On any verdict but
            :attr:`AttestationVerdict.VERIFIED`.
    """
    pin = load_pool_pin(environ)
    if pin is None:
        raise PoolAttestationError(AttestationVerdict.UNPINNED)
    try:
        response = pool_attestation_report(budget=budget, environ=environ)
    except PoolBoundaryError as exc:
        raise PoolAttestationError(AttestationVerdict.UNREACHABLE) from exc
    if not pool_response_ok(response):
        raise PoolAttestationError(AttestationVerdict.UNREACHABLE)
    report = response.payload
    if not isinstance(report, dict):
        raise PoolAttestationError(AttestationVerdict.MALFORMED)
    verdict = judge_report(report, pin)
    if verdict is not AttestationVerdict.VERIFIED:
        raise PoolAttestationError(verdict)
    return report
