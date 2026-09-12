"""Secret-free domain models for asynchronous provisioning jobs (#1768)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


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
