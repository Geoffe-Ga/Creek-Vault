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
    consumer_identity: str
    state: JobState
    operation: JobOperation
    attempts: int
    retryable: bool
    failure_reason: FailureReason | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProvisioningAllocation:
    """Durable provider allocation metadata containing no access credential."""

    allocation_id: str
    job_id: str
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
