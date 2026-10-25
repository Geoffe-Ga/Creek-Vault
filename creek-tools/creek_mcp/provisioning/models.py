"""Secret-free domain models for asynchronous provisioning jobs (#1768)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from creek_mcp.mcp_path import MCP_PATH
from creek_mcp.oauth_resource import (
    AUTHORIZATION_SERVER_ENV,
    RESOURCE_URL_ENV,
    ResourceServerConfig,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

V1_PATH: Final[str] = "/v1"
"""The published application surface every handed-off vault URL ends in."""

_STORED_MCP_OAUTH_KEYS: Final[frozenset[str]] = frozenset(
    {"resource_url", "authorization_server"}
)
_HTTPS: Final[str] = "https"


@unique
class JobState(StrEnum):
    """Externally observable provisioning lifecycle states."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    AWAITING_KEY_CEREMONY = "awaiting_key_ceremony"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


@unique
class JobOperation(StrEnum):
    """The durable provider operation a worker must perform."""

    CREATE = "create"
    DELETE = "delete"


@unique
class CustodyMode(StrEnum):
    """Truthful relationship between a durable volume and client key material."""

    PROVIDER_MANAGED = "provider_managed"
    WRAPPED_ARTIFACT_ONLY = "wrapped_artifact_only"


@unique
class FailureReason(StrEnum):
    """Stable, content-free failure reasons safe for API responses and logs."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    HANDOFF_FAILED = "handoff_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ProvisioningJob:
    """A public, secret-free snapshot of one durable provisioning job."""

    job_id: str
    activation_id: str
    requester_identity: str
    consumer_identity: str
    state: JobState
    operation: JobOperation
    attempts: int
    retryable: bool
    failure_reason: FailureReason | None
    created_at: datetime
    updated_at: datetime
    attested_confidential: bool | None = None
    custody_mode: CustodyMode | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningAllocation:
    """Durable provider allocation metadata containing no access credential."""

    allocation_id: str
    job_id: str
    requester_identity: str
    consumer_identity: str
    provider_allocation_id: str
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class RoutableAllocation:
    """The secret-free ownership record needed to route one live allocation."""

    job_id: str
    activation_id: str
    requester_identity: str
    consumer_identity: str
    provider_allocation_id: str


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A leased job plus the private token required to settle its claim."""

    job: ProvisioningJob
    lease_token: str
    provider_allocation_id: str | None = None


@unique
class ResourceClass(StrEnum):
    """Billable or access-granting provider resource classes (ADR-0013 D6)."""

    CREDENTIAL = "credential"
    MACHINE = "machine"
    VOLUME = "volume"
    APP = "app"


@unique
class ResourceState(StrEnum):
    """Coarse provider resource state as observed by fleet inventory."""

    RUNNING = "running"
    STOPPED = "stopped"
    OTHER = "other"
    DESTROYED = "destroyed"


@unique
class ReceiptOutcome(StrEnum):
    """Lifecycle of one content-free deletion receipt."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    """What a provider confirmed it removed; carries no secret or address."""

    provider: str
    resource_classes: tuple[ResourceClass, ...]

    def __post_init__(self) -> None:
        """Refuse a receipt that names no provider or no resource class."""
        if not self.provider.strip():
            raise ValueError("deletion outcome provider must not be blank")
        if not self.resource_classes:
            raise ValueError("deletion outcome must name at least one resource class")


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """Durable, content-free record of one requested deletion (ADR-0013 D6)."""

    job_id: str
    requester_identity: str
    consumer_identity: str
    provider: str | None
    provider_allocation_id: str | None
    resource_classes: tuple[ResourceClass, ...]
    requested_at: datetime
    confirmed_at: datetime | None
    outcome: ReceiptOutcome
    last_failure_reason: FailureReason | None
    attempts: int
    backfilled: bool


@dataclass(frozen=True, slots=True)
class FleetJob:
    """One job joined with its allocation and receipt for operator listings."""

    job: ProvisioningJob
    provider_allocation_id: str | None
    allocation_deleted_at: datetime | None
    delete_requested_at: datetime | None
    receipt_outcome: ReceiptOutcome | None


@unique
class DivergenceKind(StrEnum):
    """Operator-side classifications of store/provider disagreement."""

    ORPHAN_RESOURCE = "orphan_resource"
    MISSING_RESOURCE = "missing_resource"
    DUPLICATE_RESOURCE = "duplicate_resource"
    UNCONFIRMED_DELETION = "unconfirmed_deletion"
    STUCK_DELETION = "stuck_deletion"
    CONTINUOUS_RUNNING = "continuous_running"
    INCOMPLETE_CREATE = "incomplete_create"


@unique
class Disposition(StrEnum):
    """Whether the reconciler only reported a divergence or also repaired it."""

    REPORTED = "reported"
    REPAIRED = "repaired"


@dataclass(frozen=True, slots=True)
class Divergence:
    """One content-free divergence; identifiers only, never addresses or secrets."""

    kind: DivergenceKind
    disposition: Disposition
    provider_allocation_id: str | None
    resource_class: ResourceClass | None
    job_id: str | None
    age_seconds: int | None


@dataclass(frozen=True, slots=True)
class FleetTelemetry:
    """Content-free fleet measurements plus the provenance of every field."""

    activated_allocations: int
    allocations_by_state: Mapping[str, int]
    provisioned_volumes: int
    volume_bytes: int
    stopped_rootfs_gb: int
    machines_without_rootfs_size: int
    running_machine_seconds_by_allocation: Mapping[str, int]
    running_machine_seconds_fleet: int
    running_seconds_injected: int | None
    snapshot_bytes: int | None
    egress_bytes: int | None
    duplicate_allocation_attempts: int
    orphan_resources: int
    unconfirmed_deletions: int
    oldest_unconfirmed_deletion_seconds: int | None
    sources: Mapping[str, str]
    pool_input_tokens: int | None = None
    pool_output_tokens: int | None = None
    """Pool tokens the operator copied in from the vendor's usage page.

    The pool is a metered vendor line the provider API does not expose, so
    like snapshots and egress it is injected rather than measured (ADR-0016
    Decision 4).  ``None`` means uninjected, which the cost estimate lists as
    unpriced rather than zeroing.
    """


def public_vault_url(routing_public_url: str) -> str:
    """Return the ``/v1`` vault URL handed to Adepthood for a public route.

    Args:
        routing_public_url: The shared public routing origin.

    Returns:
        The origin, without a trailing slash, plus :data:`V1_PATH`.
    """
    return f"{routing_public_url.rstrip('/')}{V1_PATH}"


def mcp_resource_url(vault_url: str) -> str:
    """Return the MCP resource URL for a handed-off ``/v1`` vault URL (#1974).

    This is the one derivation both sides of the handoff use: Adepthood takes
    the vault URL it is handed, drops ``/v1`` and appends ``/mcp``, and the
    control plane writes exactly that string as the Machine's
    ``CREEK_MCP_RESOURCE_URL``, so the audience Adepthood mints for is the
    resource the vault verifies.

    Args:
        vault_url: The vault URL exactly as handed off.

    Returns:
        *vault_url* with :data:`V1_PATH` replaced by
        :data:`~creek_mcp.mcp_path.MCP_PATH`.

    Raises:
        ValueError: When *vault_url* does not end in :data:`V1_PATH`.
    """
    if not vault_url.endswith(V1_PATH):
        raise ValueError(f"a handed-off vault URL must end in {V1_PATH}")
    return f"{vault_url.removesuffix(V1_PATH)}{MCP_PATH}"


@dataclass(frozen=True, slots=True)
class McpOAuthSettings:
    """One activation's MCP resource-server settings, stored with its bundle.

    Neither value is a secret, but both ride the activation's encrypted runtime
    bundle beside the pool bundle (ADR-0016 Decision 1), so a Machine is
    verified against its *own* activation's record rather than fleet policy:
    an allocation issued before #1974 has none and keeps its exact legacy
    environment until it is rolled.  Both are kept out of ``repr`` so no log
    line built from a bundle names an allocation's address or issuer.

    Attributes:
        resource_url: The vault's public MCP URL, the exact token audience.
        authorization_server: The pinned issuer, verbatim.
    """

    resource_url: str = field(repr=False)
    authorization_server: str = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse anything the managed vault itself would refuse at boot.

        Raises:
            ValueError: When either URL fails the vault's own validation, is
                not stored verbatim, or the resource is not public ``https``.
                The message names the setting, never its value.
        """
        config = ResourceServerConfig.build(
            resource_url=self.resource_url,
            authorization_server=self.authorization_server,
        )
        if (
            config.resource_url != self.resource_url
            or config.authorization_server != self.authorization_server
        ):
            raise ValueError("MCP OAuth settings must be stored verbatim")
        if urlsplit(self.resource_url).scheme != _HTTPS:
            raise ValueError(f"{RESOURCE_URL_ENV} must use https on a managed vault")

    @classmethod
    def for_vault_url(
        cls, vault_url: str, authorization_server: str
    ) -> McpOAuthSettings:
        """Return the settings for the vault handed off at *vault_url*.

        Args:
            vault_url: The ``/v1`` vault URL handed to Adepthood.
            authorization_server: Adepthood's issuer.

        Returns:
            The settings, the resource derived by :func:`mcp_resource_url`.
        """
        return cls(mcp_resource_url(vault_url), authorization_server)

    def runtime_env(self) -> dict[str, str]:
        """Return the two Machine environment entries these settings write.

        Returns:
            ``CREEK_MCP_RESOURCE_URL`` and ``CREEK_MCP_AUTHORIZATION_SERVER``.
        """
        return {
            RESOURCE_URL_ENV: self.resource_url,
            AUTHORIZATION_SERVER_ENV: self.authorization_server,
        }

    def encode(self) -> dict[str, str]:
        """Return the JSON-ready member stored in the encrypted bundle.

        Returns:
            The two URLs under their attribute names.
        """
        return {
            "resource_url": self.resource_url,
            "authorization_server": self.authorization_server,
        }

    @classmethod
    def decode(cls, value: object) -> McpOAuthSettings | None:
        """Decode a stored member, or ``None`` for a record that has none.

        A record written before #1974 carries no member at all, which reads
        as ``None`` exactly like an explicit null: that is the migration path
        for already-provisioned allocations.

        Args:
            value: The bundle document's ``mcp_oauth`` member.

        Returns:
            The settings, or ``None``.

        Raises:
            ValueError: When the member is present but not the exact shape.
        """
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or set(value) != _STORED_MCP_OAUTH_KEYS
            or not all(isinstance(member, str) for member in value.values())
        ):
            raise ValueError("stored MCP OAuth settings are invalid")
        return cls(value["resource_url"], value["authorization_server"])
