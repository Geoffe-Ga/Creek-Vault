"""Injected provider and one-time handoff boundaries for provisioning (#1768)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from creek_mcp.provisioning.models import FailureReason


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

    def provision(self, job_id: str, consumer_identity: str) -> ProviderAllocation:
        """Idempotently create or return the allocation for *job_id*."""

    def delete(self, job_id: str, provider_allocation_id: str | None) -> None:
        """Idempotently remove every provider resource associated with *job_id*."""


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

    def provision(self, job_id: str, consumer_identity: str) -> ProviderAllocation:
        """Return one stable fake allocation for *job_id*."""
        with self._lock:
            if self._failures:
                failure = self._failures.pop(0)
                self.last_failure = failure
                raise failure
            existing = self._allocations.get(job_id)
            if existing is not None:
                return existing
            digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
            allocation = ProviderAllocation(
                allocation_id=f"fake-{digest[:24]}",
                vault_url=f"https://fake-{digest[:16]}.internal.invalid/v1",
                consumer_credential=f"fake-consumer-{consumer_identity}-{digest}",
            )
            self._allocations[job_id] = allocation
            return allocation

    def delete(self, job_id: str, provider_allocation_id: str | None) -> None:
        """Record one idempotent fake teardown without inspecting credentials."""
        del provider_allocation_id
        with self._lock:
            if job_id in self._deleted:
                return
            self._deleted.add(job_id)
            self._delete_count += 1


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
