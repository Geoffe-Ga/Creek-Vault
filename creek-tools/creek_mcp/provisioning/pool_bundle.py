"""The per-activation pool runtime bundle: identity, URL and credential (#1900).

ADR-0016 Decision 1 items 1 to 3 put the pool's model package manifest, its one
destination beyond loopback (``CREEK_POOL_URL``) and its credential in **the
activation's stored runtime bundle**, versioned per activation.  Nothing here
is a fleet constant, and :class:`~creek_mcp.provisioning.fly.FlyProviderPolicy`
carries none of it on purpose: if ``_runtime_config`` were a pure function of
the fleet policy, a re-pin would make ``_verify_machine`` reject every
not-yet-rolled Machine, taking the whole fleet down on a model change.  The
fleet-current identity therefore lives behind :class:`PoolBundleIssuer`, which
is consulted only when an activation is first issued or deliberately rolled.

**What the binding is worth.** The binding is credential to *allocation*, not
to Machine.  It stops one vault's credential being used in another vault's
session.  It does **not** protect against a holder of the organization's Fly
deploy token, who reads every ``files[].raw_value`` straight out of the Machine
config; nor against a compromised vault process, the operator, or Fly.  Under
O5 the credential is an unbound bearer: copied from a Machine config it works
from anywhere that reaches the vendor until its cap is spent.  That is why
:func:`size_pool_caps` sizes the cap to the *allowance* rather than to appetite
(Decision 1 item 3, Decision 3.4), and why a vendor without per-key caps fails
Decision 3.4 at selection time.

**Secrets discipline.** Every secret value is ``repr=False`` and no function
here returns one inside an error message, a log record or
:meth:`PoolRuntimeBundle.record`.  The operational record carries a bundle
version, a credential *kind*, the manifest's digest and the destination's host
— ids, counts and closed-vocabulary labels only (R3, D1.5, D1.12).

**Implementation neutrality.** Decision 3.5 requires that the vault-side code
not change between a pinned vendor host and a vendor's loopback sidecar, and
Decision 1 item 3 keeps the O3 bearer-plus-mutual-TLS triple as the designed
successor while O3 remains the standing fallback.  :class:`PoolCredential`
therefore carries either shape as an ordered set of guest files; only the O5
vendor-key path is built today.

No spend is authorized (Decision 4): nothing in this module dials a vendor.
The vendor admin calls of Decision 3.4 sit behind :class:`PoolBundleIssuer`,
which the attesting-provider issue (#1896) implements.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum, unique
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import urlsplit

from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from decimal import Decimal

    from creek_mcp.bench.tokens import TokensPerReflection

POOL_BUNDLE_VERSION_ENV: Final[str] = "CREEK_POOL_BUNDLE_VERSION"
"""The activation's own bundle version, so a Machine declares what it is on."""

POOL_CREDENTIAL_KIND_ENV: Final[str] = "CREEK_POOL_CREDENTIAL_KIND"
"""Which credential shape the Machine carries; a closed vocabulary."""

POOL_MANIFEST_GUEST_PATH: Final[str] = "/run/secrets/creek_model_package.json"
"""Where the manifest is mounted: outside the vault, so no vault edit loosens it."""

POOL_VENDOR_KEY_GUEST_PATH: Final[str] = "/run/secrets/creek_pool_key"
"""O5: the per-activation vendor key, in full (its update and delete need it)."""

POOL_TOKEN_GUEST_PATH: Final[str] = "/run/secrets/creek_pool_token"
"""O3 successor: the random bearer token of Decision 1 item 3."""

POOL_CLIENT_CERT_GUEST_PATH: Final[str] = "/run/secrets/creek_pool.crt"
"""O3 successor: the client certificate (``CN=<allocation_id>``, no SAN)."""

POOL_CLIENT_KEY_GUEST_PATH: Final[str] = "/run/secrets/creek_pool.key"
"""O3 successor: that certificate's private key."""

POOL_ADMIN_KEY_GUEST_PATH: Final[str] = "/run/secrets/creek_pool_admin_key"
"""The organization admin key's one declared home (#1930, Decision 3.4).

It is a *control-plane* file, never a vault one: no activation's Machine
carries it, because a vault that held it could re-cap or delete every other
vault's key.  The custody rules live in
:mod:`creek_mcp.provisioning.pool_admin_key`.
"""

POOL_ADMIN_KEY_SECRET_NAME: Final[str] = "CREEK_POOL_ADMIN_KEY_B64"
"""The Fly app secret that injects :data:`POOL_ADMIN_KEY_GUEST_PATH`."""

POOL_ADMIN_KEY_EXPIRES_AT_ENV: Final[str] = "CREEK_POOL_ADMIN_KEY_EXPIRES_AT"
"""The admin key's declared expiry: a non-secret ISO-8601 instant.

The vendor holds the real expiry and no vendor call is authorized (Decision
4), so the operator declares it beside the secret the way the Fly org token's
own expiry is declared.  An undeclared expiry is a refusal rather than an
assumption: a key nobody dated expires unnoticed and takes the per-activation
key lifecycle with it.
"""

_CAP_SAFETY_FACTOR: Final[int] = 2
"""Decision 3.4 sizes a cap at spent + allowance x measured tokens x this."""

_UNMEASURED: Final[str] = (
    "cost and cap figures must be sized from provider-reported measured tokens"
)
"""One refusal for both callers: #1914's caps and #1897's cost line."""


@unique
class PoolCredentialKind(StrEnum):
    """The credential shapes the bundle field can carry (Decision 1 item 3).

    Attributes:
        VENDOR_KEY: O5.  One per-activation vendor API key, kept in full
            because the vendor's update and delete calls require the value.
        BEARER_AND_CLIENT_TLS: The O3 successor.  A random bearer token plus a
            client certificate and its key, admitted only over that mutual-TLS
            session.  Carried by the same field; not built today.
    """

    VENDOR_KEY = "vendor_key"
    BEARER_AND_CLIENT_TLS = "bearer_and_client_tls"


_EXPECTED_PATHS: Final[dict[PoolCredentialKind, tuple[str, ...]]] = {
    PoolCredentialKind.VENDOR_KEY: (POOL_VENDOR_KEY_GUEST_PATH,),
    PoolCredentialKind.BEARER_AND_CLIENT_TLS: (
        POOL_TOKEN_GUEST_PATH,
        POOL_CLIENT_CERT_GUEST_PATH,
        POOL_CLIENT_KEY_GUEST_PATH,
    ),
}
"""The exact guest files each kind installs, in order. A closed vocabulary."""


@dataclass(frozen=True, slots=True)
class PoolCredential:
    """One activation's pool credential as an ordered set of guest files.

    The values never appear in a ``repr``, so a credential caught in a
    traceback, a log record or a job record carries its kind only.

    Attributes:
        kind: Which shape this is.
        files: ``(guest_path, value)`` pairs, exactly the paths
            :data:`_EXPECTED_PATHS` lists for *kind*, in that order.
    """

    kind: PoolCredentialKind
    files: tuple[tuple[str, bytes], ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse a credential that is incomplete for its declared kind."""
        if tuple(path for path, _ in self.files) != _EXPECTED_PATHS[self.kind]:
            raise ValueError("pool credential files do not match its kind")
        if any(not value for _, value in self.files):
            raise ValueError("pool credential values must not be empty")

    @classmethod
    def vendor_key(cls, value: str) -> PoolCredential:
        """Return the O5 credential: one vendor key, carried in full.

        Args:
            value: The vendor's full key value.

        Returns:
            The credential.
        """
        return cls(
            PoolCredentialKind.VENDOR_KEY,
            ((POOL_VENDOR_KEY_GUEST_PATH, value.encode()),),
        )

    @classmethod
    def bearer_and_client_tls(
        cls,
        *,
        token: bytes,
        certificate: bytes,
        private_key: bytes,
    ) -> PoolCredential:
        """Return the O3 successor's triple, for the designed fallback path.

        Args:
            token: The random bearer token.
            certificate: The client certificate, ``CN=<allocation_id>``.
            private_key: That certificate's private key.

        Returns:
            The credential.
        """
        return cls(
            PoolCredentialKind.BEARER_AND_CLIENT_TLS,
            (
                (POOL_TOKEN_GUEST_PATH, token),
                (POOL_CLIENT_CERT_GUEST_PATH, certificate),
                (POOL_CLIENT_KEY_GUEST_PATH, private_key),
            ),
        )

    def vendor_key_value(self) -> str:
        """Return the full vendor key, for the vendor's update and delete calls.

        Returns:
            The key value.

        Raises:
            ValueError: When this credential is not a vendor key.
        """
        if self.kind is not PoolCredentialKind.VENDOR_KEY:
            raise ValueError("pool credential is not a vendor key")
        return self.files[0][1].decode()


@dataclass(frozen=True, slots=True)
class PoolSpendCap:
    """Decision 3.4's independent per-key caps, the only external brake.

    Caps bound total spend, not rate: the per-hour limit of Decision 1 item 8
    is enforced in the vault's own limiter under every vendor.  An exhausted
    cap answers ``insufficient_quota`` (429, non-transient), so the vault
    refuses rather than spending on.

    Attributes:
        lifetime_usd: The lifetime dollar cap, or ``None`` when the vendor
            prices only in tokens.
        input_tokens: The lifetime input-token cap.
        output_tokens: The lifetime output-token cap.
        expires_at: When the key expires; extended in place by the control
            plane, so one missed cycle is survivable.  A control plane that is
            down when a key expires leaves that vault ``temporarily_unavailable``
            until it returns: fail closed, accepted.
    """

    lifetime_usd: Decimal | None
    input_tokens: int
    output_tokens: int
    expires_at: datetime

    def __post_init__(self) -> None:
        """Refuse an unbounded or time-unbounded cap triple."""
        if self.lifetime_usd is not None and self.lifetime_usd <= 0:
            raise ValueError("pool lifetime dollar cap must be positive")
        if self.input_tokens <= 0 or self.output_tokens <= 0:
            raise ValueError("pool token caps must be positive")
        if self.expires_at.tzinfo is None:
            raise ValueError("pool key expiry must be timezone-aware")


def measured_reflection_tokens(tokens: TokensPerReflection) -> tuple[int, int]:
    """Return the worst-case citable ``(prompt, output)`` tokens per reflection.

    The one basis rule for every figure sized on a measurement, in one place:
    only :attr:`~creek_mcp.bench.usage.TokenBasis.PROVIDER_REPORTED` counts
    may price a cap or a cost line.  A ``byte_upper_bound`` is a bound on
    *bytes*, and ``unreported`` and ``not_measured`` are absences; quoting any
    of them would read as a measured figure to anyone who budgeted against it.
    The worst surface is taken per direction, so a letter's larger output
    ceiling cannot overrun a figure sized on a reflection.

    Args:
        tokens: The measured per-surface token block (#1909).

    Returns:
        The worst measured prompt and output counts.

    Raises:
        ValueError: When *tokens* was not provider-reported, or reported no
            prompt or no output count at all.
    """
    if tokens.basis is not TokenBasis.PROVIDER_REPORTED:
        raise ValueError(_UNMEASURED)
    prompt = max(
        (surface.prompt_tokens or 0 for surface in tokens.per_surface.values()),
        default=0,
    )
    output = max(
        (surface.output_tokens or 0 for surface in tokens.per_surface.values()),
        default=0,
    )
    if not prompt or not output:
        raise ValueError(_UNMEASURED)
    return prompt, output


def pool_cap_tokens(
    *,
    tokens: TokensPerReflection,
    monthly_reflection_cap: int,
    input_tokens_spent: int,
    output_tokens_spent: int,
) -> tuple[int, int]:
    """Return Decision 3.4's input and output token caps, the one formula.

    ``tokens spent so far + monthly cap x measured tokens per reflection x 2``,
    independently per direction.  Extracted so the cost model (#1897) and the
    control plane quote this arithmetic rather than each re-deriving it.

    Args:
        tokens: The measured per-surface token block (#1909).
        monthly_reflection_cap: The monthly reflection allowance this vault is
            entitled to, injected rather than assumed here.
        input_tokens_spent: Input tokens this key has already spent.
        output_tokens_spent: Output tokens it has already spent.

    Returns:
        ``(input_tokens, output_tokens)``.

    Raises:
        ValueError: When *tokens* is not citable, or the allowance or spent
            counts are out of range.
    """
    prompt, output = measured_reflection_tokens(tokens)
    if monthly_reflection_cap <= 0:
        raise ValueError("monthly reflection allowance must be positive")
    if input_tokens_spent < 0 or output_tokens_spent < 0:
        raise ValueError("spent token counts must not be negative")
    allowance = monthly_reflection_cap * _CAP_SAFETY_FACTOR
    return (
        input_tokens_spent + allowance * prompt,
        output_tokens_spent + allowance * output,
    )


def size_pool_caps(
    *,
    tokens: TokensPerReflection,
    monthly_reflection_cap: int,
    input_tokens_spent: int,
    output_tokens_spent: int,
    lifetime_usd: Decimal | None,
    expires_at: datetime,
) -> PoolSpendCap:
    """Size one activation's caps to the allowance, per Decision 3.4.

    The cap is ``tokens spent so far + monthly cap x measured tokens per
    reflection x 2``, independently per direction, reset monthly by the
    vendor's update call.  The measured block (#1909) is the only admissible
    source: the ADR's 2,500 and 128 figures are a superseded planning
    assumption, and a ``byte_upper_bound`` basis is an upper bound rather than
    a measurement, so neither may price a cap.  The worst-case surface is
    taken, so a letter's larger ceiling cannot overrun a cap sized on a
    reflection.  The arithmetic itself lives in :func:`pool_cap_tokens`, so
    the cost model (#1897) reports the same numbers this sets.

    Args:
        tokens: The measured per-surface token block.
        monthly_reflection_cap: The monthly reflection allowance this vault is
            entitled to (adepthood's ``DEFAULT_MONTHLY_CAP``, injected rather
            than assumed here).
        input_tokens_spent: Input tokens this key has already spent, from the
            vendor's key listing.
        output_tokens_spent: Output tokens it has already spent.
        lifetime_usd: The dollar cap to set alongside, or ``None``.
        expires_at: The key's expiry.

    Returns:
        The cap triple to set on the vendor key.

    Raises:
        ValueError: When *tokens* was not provider-reported, or the allowance
            or spent counts are negative.
    """
    input_tokens, output_tokens = pool_cap_tokens(
        tokens=tokens,
        monthly_reflection_cap=monthly_reflection_cap,
        input_tokens_spent=input_tokens_spent,
        output_tokens_spent=output_tokens_spent,
    )
    return PoolSpendCap(
        lifetime_usd=lifetime_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class PoolRuntimeBundle:
    """One activation's stored pool bundle, versioned per activation.

    ``_verify_machine`` compares each Machine against *this* — the version its
    own activation stores — never against a fleet constant, so a fleet re-pin
    rejects nothing until the activation is deliberately rolled (Decision 1
    item 1, T12, T26).

    Attributes:
        bundle_version: This activation's bundle version, monotonic per
            activation.  A roll writes the next one.
        pool_url: The one destination beyond loopback: under O5 the exact
            vendor hostname pinned in the manifest, under a loopback sidecar
            the sidecar's address whose single egress the same key pins.  No
            vault config value can set or change it.
        manifest_json: The #1855 model package manifest, schema unchanged.
        credential: This activation's credential.
    """

    bundle_version: int
    pool_url: str
    manifest_json: str = field(repr=False)
    credential: PoolCredential = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse an unversioned, destination-less or empty bundle."""
        if self.bundle_version <= 0:
            raise ValueError("pool bundle version must be positive")
        if not self.manifest_json.strip():
            raise ValueError("pool bundle manifest must not be blank")
        parts = urlsplit(self.pool_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("pool bundle URL must be an absolute HTTP(S) URL")

    def manifest_digest(self) -> str:
        """Return the manifest's SHA-256, the only citable form in a record.

        Returns:
            The lowercase hex digest.
        """
        return hashlib.sha256(self.manifest_json.encode()).hexdigest()

    def record(self) -> dict[str, object]:
        """Return the content-free operational record for this bundle.

        D1.12 allows ids, counts, closed-vocabulary labels and policy or build
        versions only.  The credential, the manifest body and anything a
        prompt could be guessed from are deliberately absent.

        Returns:
            The record.
        """
        return {
            "bundle_version": self.bundle_version,
            "credential_kind": self.credential.kind.value,
            "manifest_digest": self.manifest_digest(),
            "pool_url_host": urlsplit(self.pool_url).hostname,
        }


class PoolBundleIssuer(Protocol):
    """Mint and revoke one activation's pool bundle at the fleet identity.

    The fleet-current identity (version, destination, manifest) lives behind
    this boundary, so the provider driver never reads it: that is what keeps
    ``_verify_machine`` per-activation.  Every vendor-specific call sits here
    too, behind the adapter interface the attesting-provider issue (#1896)
    owns, so this work lands before the vendor is chosen.
    """

    def issue(self, allocation_id: str) -> PoolRuntimeBundle:
        """Return a new bundle for *allocation_id* at the fleet identity."""

    def revoke(self, bundle: PoolRuntimeBundle) -> None:
        """Revoke *bundle*'s credential, confirming the pool now refuses it.

        It must raise rather than return when the revocation is unconfirmed:
        ``delete()`` calls this before destroying the Machine and before the
        content-free receipt is issued (Decision 1 item 3, T11).
        """


@dataclass(frozen=True, slots=True)
class PoolCredentialRecord:
    """One pool credential as inventory sees it: identifiers only.

    Attributes:
        provider_allocation_id: The allocation the credential belongs to.
        provider_ref: The provider's own reference for it, never its value.
    """

    provider_allocation_id: str
    provider_ref: str


class PoolCredentialInventory(Protocol):
    """Read-only listing of live pool credentials, for fleet reconciliation.

    Without it a credential that survived a deletion would be invisible to the
    fleet report; with it the existing orphan path flags it (T11).
    """

    def list_pool_credentials(
        self,
        allocation_ids: Sequence[str],
    ) -> tuple[PoolCredentialRecord, ...]:
        """Return every live credential under the bounded known set."""


def pool_runtime_env(bundle: PoolRuntimeBundle) -> dict[str, str]:
    """Return the Machine ``env`` entries this bundle contributes.

    Args:
        bundle: The activation's stored bundle.

    Returns:
        The environment entries, on either runtime.  Secret *values* are
        never among them: the credential travels as a file.
    """
    return {
        POOL_URL_ENV: bundle.pool_url,
        MODEL_PACKAGE_FILE_ENV: POOL_MANIFEST_GUEST_PATH,
        POOL_BUNDLE_VERSION_ENV: str(bundle.bundle_version),
        POOL_CREDENTIAL_KIND_ENV: bundle.credential.kind.value,
    }


def pool_machine_files(bundle: PoolRuntimeBundle) -> tuple[tuple[str, bytes], ...]:
    """Return the Machine ``files`` entries this bundle contributes.

    Args:
        bundle: The activation's stored bundle.

    Returns:
        ``(guest_path, value)`` pairs: the manifest first, then the
        credential's own files in their declared order.
    """
    return (
        (POOL_MANIFEST_GUEST_PATH, bundle.manifest_json.encode()),
        *bundle.credential.files,
    )


def pool_secret_files(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Return the pool files the replay bootstrap must normalize.

    The bootstrap *validates listed files*; it never enumerates
    ``/run/secrets`` (Decision 6).  The list is derived from a closed
    vocabulary the Machine declares in its own environment, so a vault with no
    pool normalizes nothing and a vault with one normalizes exactly its own
    credential shape.

    Args:
        environ: The Machine's environment.

    Returns:
        The paths, empty when no pool is configured.

    Raises:
        ValueError: When a pool is configured under an unknown credential kind.
    """
    if not environ.get(POOL_URL_ENV, "").strip():
        return ()
    declared = environ.get(POOL_CREDENTIAL_KIND_ENV, "").strip()
    try:
        kind = PoolCredentialKind(declared)
    except ValueError:
        raise ValueError("pool credential kind is not recognized") from None
    return (
        Path(POOL_MANIFEST_GUEST_PATH),
        *(Path(path) for path in _EXPECTED_PATHS[kind]),
    )
