"""Injected provider and one-time handoff boundaries for provisioning (#1768)."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from itertools import chain
from threading import Lock
from typing import TYPE_CHECKING, Final, Protocol

from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    FailureReason,
    ResourceClass,
    ResourceState,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek_mcp.provisioning.models import ProvisioningJob

_FAKE_PROVIDER: Final[str] = "fake"
_FAKE_ROOTFS_GB: Final[int] = 1
_FAKE_VOLUME_GB: Final[int] = 5
_FAKE_DIGEST_LENGTH: Final[int] = 24
_FAKE_RESOURCE_CLASSES: Final[tuple[ResourceClass, ...]] = (
    ResourceClass.CREDENTIAL,
    ResourceClass.MACHINE,
    ResourceClass.VOLUME,
    ResourceClass.APP,
)


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
    ) -> DeletionOutcome:
        """Remove every provider resource of *job*; return only what was confirmed."""


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
        self._delete_count = 0
        self._resources: dict[str, list[ProviderResource]] = {}
        self._activation_allocations: dict[str, str] = {}
        self._inventory_calls = 0
        self._stopped: list[str] = []
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

    @property
    def inventory_call_count(self) -> int:
        """Return how many times the fleet inventory was listed."""
        with self._lock:
            return self._inventory_calls

    @property
    def stop_count(self) -> int:
        """Return how many stop calls the fake honoured."""
        with self._lock:
            return len(self._stopped)

    @property
    def stopped_activation_ids(self) -> tuple[str, ...]:
        """Return the activation ids stopped, in call order."""
        with self._lock:
            return tuple(self._stopped)

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
            allocation_id = f"fake-{digest[:_FAKE_DIGEST_LENGTH]}"
            allocation = ProviderAllocation(
                allocation_id=allocation_id,
                vault_url=f"https://fake-{digest[:16]}.internal.invalid/v1",
                consumer_credential=(f"fake-consumer-{job.consumer_identity}-{digest}"),
            )
            self._allocations[job.job_id] = allocation
            self._activation_allocations[job.activation_id] = allocation_id
            self._resources[allocation_id] = _fake_resources(
                allocation_id, job.activation_id
            )
            return allocation

    def delete(
        self,
        job: ProvisioningJob,
        provider_allocation_id: str | None,
    ) -> DeletionOutcome:
        """Record one idempotent fake teardown without inspecting credentials."""
        del provider_allocation_id
        with self._lock:
            if job.job_id not in self._deleted:
                self._deleted.add(job.job_id)
                self._delete_count += 1
            existing = self._allocations.get(job.job_id)
            if existing is not None:
                self._resources.pop(existing.allocation_id, None)
        return DeletionOutcome(_FAKE_PROVIDER, _FAKE_RESOURCE_CLASSES)

    def list_resources(
        self,
        activation_ids: Sequence[str],
        *,
        app_names: Sequence[str] = (),
    ) -> tuple[ProviderResource, ...]:
        """Return every created or seeded resource; the fake ignores its scope."""
        del activation_ids, app_names
        with self._lock:
            self._inventory_calls += 1
            return tuple(
                sorted(
                    chain.from_iterable(self._resources.values()),
                    key=lambda r: (
                        r.provider_allocation_id,
                        r.resource_class.value,
                        r.provider_ref,
                    ),
                )
            )

    def expected_allocation_id(self, activation_id: str) -> str:
        """Return the allocation id *activation_id* has or would receive."""
        with self._lock:
            known = self._activation_allocations.get(activation_id)
        if known is not None:
            return known
        digest = hashlib.sha256(activation_id.encode("utf-8")).hexdigest()
        return f"fake-{digest[:_FAKE_DIGEST_LENGTH]}"

    def stop(self, activation_id: str) -> None:
        """Stop the allocation's Machine; unknown allocations are unavailable."""
        allocation_id = self.expected_allocation_id(activation_id)
        with self._lock:
            if not self._set_machine_state(allocation_id, ResourceState.STOPPED):
                raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)
            self._stopped.append(activation_id)

    def seed_resource(self, resource: ProviderResource) -> None:
        """Add one provider-side resource the control plane did not create."""
        with self._lock:
            self._resources.setdefault(resource.provider_allocation_id, []).append(
                resource
            )

    def set_machine_state(
        self,
        provider_allocation_id: str,
        state: ResourceState,
    ) -> None:
        """Flip every Machine under one allocation to *state* (test seam)."""
        with self._lock:
            self._set_machine_state(provider_allocation_id, state)

    def has_resource(self, provider_allocation_id: str) -> bool:
        """Return whether any resource survives under *provider_allocation_id*."""
        with self._lock:
            return bool(self._resources.get(provider_allocation_id))

    def _set_machine_state(
        self,
        provider_allocation_id: str,
        state: ResourceState,
    ) -> bool:
        """Replace Machine states under the lock; return whether any existed."""
        resources = self._resources.get(provider_allocation_id, [])
        machines = [r for r in resources if r.resource_class is ResourceClass.MACHINE]
        for machine in machines:
            resources[resources.index(machine)] = dataclasses.replace(
                machine, state=state
            )
        return bool(machines)


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


def _fake_resources(allocation_id: str, activation_id: str) -> list[ProviderResource]:
    """Return the app, stopped Machine, and volume one fake provision creates."""
    return [
        ProviderResource(
            allocation_id,
            ResourceClass.APP,
            ResourceState.OTHER,
            None,
            None,
            f"{allocation_id}-app",
        ),
        ProviderResource(
            allocation_id,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            _FAKE_ROOTFS_GB,
            activation_id,
            f"{allocation_id}-machine",
        ),
        ProviderResource(
            allocation_id,
            ResourceClass.VOLUME,
            ResourceState.OTHER,
            _FAKE_VOLUME_GB,
            None,
            f"{allocation_id}-volume",
        ),
    ]
