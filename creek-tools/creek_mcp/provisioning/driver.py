"""Injected provider and one-time handoff boundaries for provisioning (#1768)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Final, Protocol

from creek_mcp.provisioning.inventory import (
    InventorySnapshot,
    ProviderResource,
    ProviderResourceClass,
)

if TYPE_CHECKING:
    from creek_mcp.provisioning.models import FailureReason, ProvisioningJob

_FAKE_RESOURCE_CLASSES: Final[tuple[ProviderResourceClass, ...]] = (
    ProviderResourceClass.APP,
    ProviderResourceClass.MACHINE,
    ProviderResourceClass.VOLUME,
)
"""What one fake allocation consists of, mirroring Decision 3's one-of-each."""


@dataclass(frozen=True, slots=True)
class ProviderAllocation:
    """Internal provider result whose credential is deliberately absent from repr."""

    allocation_id: str
    vault_url: str
    consumer_credential: str = field(repr=False)


class ProviderError(RuntimeError):
    """A provider failure carrying only a stable public reason and retry policy."""

    def __init__(
        self,
        reason: FailureReason,
        *,
        retryable: bool,
        private_detail: str | None = None,
    ) -> None:
        """Keep private provider detail off the exception's public representation."""
        super().__init__(f"provider operation failed: {reason.value}")
        self.reason = reason
        self.retryable = retryable
        del private_detail


class HandoffError(RuntimeError):
    """The internal credential sink refused a conflicting one-time delivery."""


class ProviderDriver(Protocol):
    """Provider operations injected into the durable worker."""

    def provision(self, job: ProvisioningJob) -> ProviderAllocation:
        """Idempotently create or return the allocation for *job*."""

    def delete(
        self,
        job: ProvisioningJob,
        provider_allocation_id: str | None,
    ) -> None:
        """Idempotently remove every provider resource associated with *job*."""


class OneTimeCredentialHandoff(Protocol):
    """Internal-only sink that accepts a credential once per durable job."""

    def deliver(
        self,
        job_id: str,
        consumer_identity: str,
        vault_url: str,
        consumer_credential: str,
    ) -> None:
        """Deliver or idempotently acknowledge one identical prior delivery."""


class FakeProviderDriver:
    """Thread-safe idempotent fake used by contract tests, never a cloud adapter."""

    def __init__(self) -> None:
        """Initialize an empty fake provider account."""
        self._lock = Lock()
        self._allocations: dict[str, ProviderAllocation] = {}
        self._failures: list[ProviderError] = []
        self._deleted: set[str] = set()
        self._orphans: set[str] = set()
        self._delete_count = 0
        self.last_failure: ProviderError | None = None

    @property
    def allocation_count(self) -> int:
        """Return the number of allocations the fake has ever created."""
        with self._lock:
            return len(self._allocations)

    @property
    def delete_count(self) -> int:
        """Return the number of distinct provider teardowns performed."""
        with self._lock:
            return self._delete_count

    def fail_next(self, failure: ProviderError) -> None:
        """Queue one deterministic provider failure for the next create call."""
        with self._lock:
            self._failures.append(failure)

    def provision(self, job: ProvisioningJob) -> ProviderAllocation:
        """Return one stable fake allocation for *job*."""
        with self._lock:
            if self._failures:
                failure = self._failures.pop(0)
                self.last_failure = failure
                raise failure
            existing = self._allocations.get(job.job_id)
            if existing is not None:
                return existing
            digest = hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()
            allocation = ProviderAllocation(
                allocation_id=f"fake-{digest[:24]}",
                vault_url=f"https://fake-{digest[:16]}.internal.invalid/v1",
                consumer_credential=(f"fake-consumer-{job.consumer_identity}-{digest}"),
            )
            self._allocations[job.job_id] = allocation
            return allocation

    def delete(
        self,
        job: ProvisioningJob,
        provider_allocation_id: str | None,
    ) -> None:
        """Record one idempotent fake teardown without inspecting credentials."""
        del provider_allocation_id
        with self._lock:
            if job.job_id in self._deleted:
                return
            self._deleted.add(job.job_id)
            self._delete_count += 1

    def adopt_orphan(self, provider_allocation_id: str) -> None:
        """Plant provider state the durable store has never recorded (#1769).

        Fleet reconciliation's hardest case is a resource the control plane
        cannot name — a create that billed before its row was written, or a
        delete that half-succeeded. A store-free seam is the only way to build
        that arrangement without corrupting the store to fake it.
        """
        with self._lock:
            self._orphans.add(provider_allocation_id)

    def list_resources(self) -> InventorySnapshot:
        """Enumerate every allocation this fake account still bills for.

        Satisfies :class:`~creek_mcp.provisioning.inventory.ProviderInventory`.
        Read-only by construction: it mutates nothing, so a reconciliation pass
        driven by this fake cannot repair anything either. The fake account is
        always fully readable, so the snapshot is always complete.
        """
        with self._lock:
            live = {
                allocation.allocation_id
                for job_id, allocation in self._allocations.items()
                if job_id not in self._deleted
            }
            surrogates = sorted(live | self._orphans)
        return InventorySnapshot(
            resources=tuple(
                ProviderResource(
                    resource_class=resource_class,
                    provider_id=f"{surrogate}-{resource_class.value}",
                    provider_allocation_id=surrogate,
                    state="stopped",
                )
                for surrogate in surrogates
                for resource_class in _FAKE_RESOURCE_CLASSES
            ),
            complete=True,
        )


class FakeOneTimeHandoff:
    """A secret-discarding idempotent handoff sink for contract tests."""

    def __init__(self) -> None:
        """Initialize an empty delivery ledger containing fingerprints only."""
        self._lock = Lock()
        self._fingerprints: dict[str, bytes] = {}

    @property
    def delivery_count(self) -> int:
        """Return how many distinct jobs were accepted by the sink."""
        with self._lock:
            return len(self._fingerprints)

    def deliver(
        self,
        job_id: str,
        consumer_identity: str,
        vault_url: str,
        consumer_credential: str,
    ) -> None:
        """Accept one delivery while retaining no credential plaintext."""
        payload = "\0".join(
            (job_id, consumer_identity, vault_url, consumer_credential)
        ).encode("utf-8")
        fingerprint = hashlib.sha256(payload).digest()
        with self._lock:
            existing = self._fingerprints.get(job_id)
            if existing is None:
                self._fingerprints[job_id] = fingerprint
                return
            if existing != fingerprint:
                raise HandoffError("credential handoff conflicts with prior delivery")
