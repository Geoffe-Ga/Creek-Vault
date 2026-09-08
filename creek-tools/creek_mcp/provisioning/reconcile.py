"""Report-only fleet reconciliation over store and provider snapshots (#1769).

ADR-0013 Decision 6 requires that every provider resource Creek pays for is
reconciled against the durable control plane, and that a deletion the provider
never confirmed stays visible until it does. This module answers that by
comparing two snapshots — the operator-scoped store view and a
:class:`~creek_mcp.provisioning.inventory.ProviderInventory` read — and
classifying every disagreement into one closed enum.

Three properties are structural rather than asserted.

*Report only.* :class:`ReconcileMode` has exactly one member. There is no
``delete_orphan``, no ``start`` and no ``stop`` anywhere in this module, and
the reconciler is typed on ``ProviderInventory``, which cannot mutate anything.
That is a deliberate ruling, not an omission: Decision 6 binds resource removal
to revoking the consumer credential, ``FlySecretManager.revoke`` is keyed on
the ``activation_id``, and an orphan is identified only by
``fly-<sha256(activation)[:24]>``. An automatic repair path would therefore
destroy billable resources while leaving a live credential issued — strictly
worse than the orphan it was cleaning up. The runbook carries a manual
procedure instead.

*Purity.* :meth:`FleetReconciler.reconcile` is a function of (store snapshot,
inventory snapshot, clock) and writes nothing durable, so two passes over
unchanged state return equal reports and a crash mid-pass leaves no residue.
``observed_at`` is excluded from equality so the clock cannot make two
otherwise identical reports differ.

*Content-freedom.* Every subject is a provider allocation surrogate. The
plaintext activation id that ``FlyProviderDriver`` writes into Machine metadata
is never read, never selected by the operator store queries, and has no field
here that could hold it.

Four judgement calls, stated so they are known gaps rather than silent ones.

1. ``MISSING_PROVIDER_RESOURCE`` is decided at **app granularity**: an
   allocation whose app is still listed is not reported, even if the volume or
   Machine underneath it has vanished. A volume that disappears under a live
   app is therefore invisible to this pass.
2. ``_missing`` skips allocations whose operation is ``delete``. A deletion in
   flight is not a resource still expected to exist; it is reported as
   ``DELETION_UNCONFIRMED`` once it ages past the window.
3. ``_duplicates`` and ``_running`` only consider allocations the store still
   holds. An orphan's second volume is reported once, as an orphan, rather than
   twice.
4. ``RUNNING_BEYOND_POLICY`` measures from Fly's ``updated_at``, a **lower
   bound** on uptime that any provider write resets. Machines whose meter
   cannot be read at all are not silently treated as compliant: they are named
   in :attr:`FleetReconciliationReport.unmetered_running`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.inventory import MetricQuality, ProviderResourceClass
from creek_mcp.provisioning.models import JobOperation

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime

    from creek_mcp.provisioning.inventory import ProviderInventory, ProviderResource
    from creek_mcp.provisioning.models import OperatorAllocationView
    from creek_mcp.provisioning.store import ProvisioningStore

_RUNNING_STATES: Final[frozenset[str]] = frozenset({"started", "starting"})
"""Fly Machine states that bill for CPU, mirroring FlyProviderDriver.start."""


@unique
class DivergenceKind(StrEnum):
    """The closed set of disagreements one reconciliation pass can observe."""

    ORPHAN_PROVIDER_RESOURCE = "orphan_provider_resource"
    MISSING_PROVIDER_RESOURCE = "missing_provider_resource"
    DUPLICATE_ALLOCATION = "duplicate_allocation"
    DELETION_UNCONFIRMED = "deletion_unconfirmed"
    RUNNING_BEYOND_POLICY = "running_beyond_policy"


@unique
class ReconcileMode(StrEnum):
    """Reconciliation authority. #1769 ships exactly one member, by ruling."""

    REPORT_ONLY = "report_only"


class FleetReconciliationError(RuntimeError):
    """A provider resource fell outside the closed classification."""


@dataclass(frozen=True, slots=True)
class FleetReconcilePolicy:
    """Injected reconciliation thresholds; none of them is a protocol constant."""

    unconfirmed_deletion_after: timedelta
    max_continuous_running: timedelta

    def __post_init__(self) -> None:
        """Reject thresholds that would make a divergence unobservable."""
        if self.unconfirmed_deletion_after < timedelta(0):
            raise ValueError("unconfirmed_deletion_after must not be negative")
        if self.max_continuous_running <= timedelta(0):
            raise ValueError("max_continuous_running must be positive")


@dataclass(frozen=True, slots=True)
class FleetDivergence:
    """One content-free divergence naming a surrogate, never an activation id."""

    kind: DivergenceKind
    subject: str
    resource_class: ProviderResourceClass | None = None
    provider_id: str | None = None


@dataclass(frozen=True, slots=True)
class FleetReconciliationReport:
    """A deterministic, content-free snapshot of one reconciliation pass.

    ``unmetered_running`` names the live allocations holding a Machine that is
    running but whose uptime meter could not be read. They are neither reported
    as compliant nor as divergent, because neither is known — and an operator
    who cannot see them has no way to tell the difference.
    """

    mode: ReconcileMode
    inventory_complete: bool
    divergences: tuple[FleetDivergence, ...]
    unmetered_running: tuple[str, ...]
    observed_at: datetime = field(compare=False)


def _sort_key(divergence: FleetDivergence) -> tuple[str, str, str, str]:
    """Return one total, content-free ordering so two passes compare equal."""
    resource_class = divergence.resource_class
    return (
        divergence.kind.value,
        divergence.subject,
        "" if resource_class is None else resource_class.value,
        divergence.provider_id or "",
    )


def _is_unique_per_allocation(resource_class: ProviderResourceClass) -> bool:
    """Return whether one allocation may hold at most one of *resource_class*.

    Decision 3 fixes an allocation at one app, one Machine and one encrypted
    volume, and ``FlyProviderDriver._only`` refuses a second Machine or volume
    at provision time. Snapshots are unbounded by design. The final arm is
    unreachable through the enum and raises rather than defaulting, so adding a
    resource class fails loudly here instead of silently escaping the report.
    """
    match resource_class:
        case ProviderResourceClass.MACHINE | ProviderResourceClass.VOLUME:
            return True
        case ProviderResourceClass.APP | ProviderResourceClass.SNAPSHOT:
            return False
        case _:
            raise FleetReconciliationError("provider resource class is unclassifiable")


class FleetReconciler:
    """Compare durable allocations against provider truth, and only report."""

    def __init__(
        self,
        store: ProvisioningStore,
        inventory: ProviderInventory,
        policy: FleetReconcilePolicy,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind read-only store and inventory boundaries plus an injected clock."""
        self._store = store
        self._inventory = inventory
        self._policy = policy
        self._clock = clock

    def reconcile(self) -> FleetReconciliationReport:
        """Return one deterministic report, having mutated no provider resource."""
        now = self._clock()
        allocations = self._store.live_allocations()
        resources, complete = self._snapshot()
        live = {allocation.provider_allocation_id for allocation in allocations}
        divergences = [
            *self._orphans(resources, live),
            *self._missing(allocations, resources, complete=complete),
            *self._duplicates(resources, live),
            *self._unconfirmed(now),
            *self._running(resources, live, now=now),
        ]
        return FleetReconciliationReport(
            mode=ReconcileMode.REPORT_ONLY,
            inventory_complete=complete,
            divergences=tuple(sorted(divergences, key=_sort_key)),
            unmetered_running=self._unmetered(resources, live),
            observed_at=now,
        )

    def _snapshot(self) -> tuple[tuple[ProviderResource, ...], bool]:
        """Read the inventory, keeping whatever a partial pass did observe.

        A boundary that raises rather than reporting partiality is still
        handled, because ``ProviderInventory`` is an injected Protocol and a
        third-party implementation may do so — but nothing is inferred from
        the resulting emptiness beyond ``complete`` being False.
        """
        try:
            snapshot = self._inventory.list_resources()
        except ProviderError:
            return (), False
        return snapshot.resources, snapshot.complete

    @staticmethod
    def _orphans(
        resources: Sequence[ProviderResource],
        live: set[str],
    ) -> Iterator[FleetDivergence]:
        """Report each resource no live allocation record accounts for."""
        for resource in resources:
            surrogate = resource.provider_allocation_id
            if surrogate is None or surrogate in live:
                continue
            yield FleetDivergence(
                kind=DivergenceKind.ORPHAN_PROVIDER_RESOURCE,
                subject=surrogate,
                resource_class=resource.resource_class,
                provider_id=resource.provider_id,
            )

    @staticmethod
    def _missing(
        allocations: Sequence[OperatorAllocationView],
        resources: Sequence[ProviderResource],
        *,
        complete: bool,
    ) -> Iterator[FleetDivergence]:
        """Report live allocations the provider no longer shows an app for.

        Suppressed entirely when the inventory read did not complete: an org
        listing that rate-limited halfway would otherwise report every
        allocation in the fleet as vanished, and that report is the input to
        alerting and to any operator-initiated removal.
        """
        if not complete:
            return
        observed = {
            resource.provider_allocation_id
            for resource in resources
            if resource.resource_class is ProviderResourceClass.APP
        }
        for allocation in allocations:
            # A deletion already in flight is not a resource we still expect;
            # it is reported as DELETION_UNCONFIRMED once it goes stale.
            if allocation.operation is JobOperation.DELETE:
                continue
            if allocation.provider_allocation_id not in observed:
                yield FleetDivergence(
                    kind=DivergenceKind.MISSING_PROVIDER_RESOURCE,
                    subject=allocation.provider_allocation_id,
                )

    @staticmethod
    def _duplicates(
        resources: Sequence[ProviderResource],
        live: set[str],
    ) -> Iterator[FleetDivergence]:
        """Report a second billable Machine or volume under one allocation."""
        grouped: defaultdict[tuple[str, ProviderResourceClass], list[str]] = (
            defaultdict(list)
        )
        for resource in resources:
            surrogate = resource.provider_allocation_id
            if surrogate is None or surrogate not in live:
                continue
            if _is_unique_per_allocation(resource.resource_class):
                grouped[(surrogate, resource.resource_class)].append(
                    resource.provider_id
                )
        for (surrogate, resource_class), provider_ids in grouped.items():
            if len(provider_ids) > 1:
                yield from (
                    FleetDivergence(
                        kind=DivergenceKind.DUPLICATE_ALLOCATION,
                        subject=surrogate,
                        resource_class=resource_class,
                        provider_id=provider_id,
                    )
                    for provider_id in provider_ids
                )

    def _unconfirmed(self, now: datetime) -> Iterator[FleetDivergence]:
        """Report deletions the provider has not confirmed within the window."""
        for allocation in self._store.unconfirmed_deletions(
            self._policy.unconfirmed_deletion_after,
            now=now,
        ):
            yield FleetDivergence(
                kind=DivergenceKind.DELETION_UNCONFIRMED,
                subject=allocation.provider_allocation_id,
            )

    @staticmethod
    def _unmetered(
        resources: Sequence[ProviderResource],
        live: set[str],
    ) -> tuple[str, ...]:
        """Name live allocations whose running Machine has no readable meter."""
        return tuple(
            sorted(
                {
                    resource.provider_allocation_id
                    for resource in resources
                    if resource.provider_allocation_id is not None
                    and resource.provider_allocation_id in live
                    and resource.resource_class is ProviderResourceClass.MACHINE
                    and resource.state in _RUNNING_STATES
                    and resource.last_modified_quality is not MetricQuality.EXACT
                }
            )
        )

    def _running(
        self,
        resources: Sequence[ProviderResource],
        live: set[str],
        *,
        now: datetime,
    ) -> Iterator[FleetDivergence]:
        """Report a Machine billing for CPU past the configured window."""
        deadline = now - self._policy.max_continuous_running
        for resource in resources:
            surrogate = resource.provider_allocation_id
            if surrogate is None or surrogate not in live:
                continue
            if not self._is_running_beyond(resource, deadline):
                continue
            yield FleetDivergence(
                kind=DivergenceKind.RUNNING_BEYOND_POLICY,
                subject=surrogate,
                resource_class=resource.resource_class,
                provider_id=resource.provider_id,
            )

    @staticmethod
    def _is_running_beyond(resource: ProviderResource, deadline: datetime) -> bool:
        """Return whether *resource* has billed for CPU since before *deadline*.

        The measure is a **lower bound**, not the true uptime.
        ``last_modified_at`` is Fly's ``updated_at``, which any provider-side
        write resets, so a Machine that has run for a week but was touched an
        hour ago reads as an hour old. The window can therefore under-report;
        it cannot over-report, which is the safe direction for a signal an
        operator acts on. Parsing Fly's ``events[]`` for the true transition is
        deliberately out of scope here.

        A Machine the provider gives no readable timestamp for is not reported,
        because guessing how long it has run would put a fabricated duration
        into an operator's report — those Machines are surfaced instead through
        :attr:`FleetReconciliationReport.unmetered_running`.
        """
        return (
            resource.resource_class is ProviderResourceClass.MACHINE
            and resource.state in _RUNNING_STATES
            and resource.last_modified_at is not None
            and resource.last_modified_at <= deadline
        )
