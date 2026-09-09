"""Report-only fleet cost telemetry and injected budget configuration (#1769).

ADR-0013 Decision 4 requires the control plane to *report* provisioned volumes,
stopped rootfs capacity, active Machine seconds, snapshot bytes and egress so an
operator can reconcile an actual invoice, and it states that the reference
prices "belong in operations documentation and billing tests as injected
assumptions, never as business logic". This module is the reporting half. Not
one price, budget or conversion base appears in it: :class:`FleetPriceTable`
has no default on any field, so a figure cannot be obtained without an operator
supplying one, and the ADR's own numbers live only in
``docs/provisioning-control-plane.md`` and in ``tests/test_fleet_telemetry.py``.

Four properties are structural rather than asserted.

*Report only.* :class:`FleetTelemetry` is typed on ``ProviderInventory``, which
cannot mutate anything, and composes the report-only reconciler. There is no
``start``, ``stop``, ``delete`` or repair path anywhere here. PR2 measures;
alarms are the next slice's, and nothing below compares a meter to
:attr:`FleetPriceTable.monthly_budget`.

*A meter that could not be read is never a silent zero.* :class:`Meter` refuses
to exist unless ``value is None`` and the quality being ``UNAVAILABLE`` agree,
and every provider-sourced figure runs through one combinator, :func:`_measured`,
whose first arm is the important one: contributors that exist but of which none
is readable yield ``Meter(None, UNAVAILABLE)``, not a compliant-looking zero.
Zero contributors on a *complete* enumeration is the only legitimate zero.

*Machine seconds are an instantaneous lower bound, not a billing-period total.*
The only clock the provider gives us is Fly's ``updated_at``, which any
provider-side write resets, and parsing ``events[]`` for the true transition is
out of scope exactly as it is for the reconciler. So the figure is labelled
``ESTIMATED`` whenever it exists — including when it is 0, because the quality
follows the meter's semantics and not its data — and it describes *this instant*,
never an accumulated month. Earning a real billing-period figure needs persisted
samples, a sampler process and a crash-safety argument; that is a separate slice
with its own schema change. This is why :func:`estimate_monthly_cost`
deliberately does **not** consume a snapshot: feeding a lower-bound duration
into a function whose output is labelled a monthly cost would fabricate a
figure.

*Egress is unobtainable from the provider API.* Fly meters bytes out on its
billing surface, not the Machines API, so it arrives through a third injected
Protocol, :class:`EgressMeter`, whose shipped implementation answers
``UNAVAILABLE`` honestly. Enumeration and provisioning stay narrow in opposite
directions and egress widens neither.

Two known gaps, stated so they are not silent ones.

1. Composing :class:`~creek_mcp.provisioning.reconcile.FleetReconciler` means
   ``store.live_allocations()`` executes **twice** per :meth:`FleetTelemetry.observe`.
   Both reads are read-only, but they are not one transaction, so a job that
   settles between them makes one pass internally inconsistent. Folding them
   would require the reconciler to accept or expose its allocations, and its
   import surface is pinned by exact equality.
2. Items 6 and 7 — orphan provider resources and unconfirmed deletions — are
   counted off the reconciler's own ``divergences`` rather than re-derived, so
   nothing downstream can disagree with the reconciler about what an orphan is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.inventory import (
    InventorySnapshot,
    MetricQuality,
    ProviderResourceClass,
)
from creek_mcp.provisioning.reconcile import (
    _RUNNING_STATES,
    DivergenceKind,
    FleetReconciler,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from creek_mcp.provisioning.inventory import ProviderInventory, ProviderResource
    from creek_mcp.provisioning.reconcile import (
        FleetReconcilePolicy,
        FleetReconciliationReport,
    )
    from creek_mcp.provisioning.store import ProvisioningStore


@dataclass(frozen=True, slots=True)
class Meter:
    """One reported figure that always says how far it can be trusted.

    The invariant is enforced in both directions: an absent value must be
    ``UNAVAILABLE`` and an ``UNAVAILABLE`` quality must have no value. That
    makes "a meter that could not be read reported as a compliant zero" —
    the failure :class:`~creek_mcp.provisioning.inventory.MetricQuality`
    exists to prevent — unconstructable rather than merely discouraged, and
    it stays enforced for every field a later slice adds.
    """

    value: int | None
    quality: MetricQuality

    def __post_init__(self) -> None:
        """Refuse a value and a quality that disagree about being readable."""
        if (self.value is None) != (self.quality is MetricQuality.UNAVAILABLE):
            raise ValueError("a meter has a value if and only if it is not unavailable")


@dataclass(frozen=True, slots=True)
class AllocationMeter:
    """One meter attributed to a provider surrogate, never to an activation.

    A sorted tuple of these is used rather than a mapping so two passes over
    unchanged state compare equal, and so no consumer is tempted to key the
    figure by a consumer identity.
    """

    subject: str
    meter: Meter


class EgressMeter(Protocol):
    """A read-only boundary reporting bytes out of one provider account.

    Declared here as its own capability rather than added to
    ``ProviderInventory``: the Fly Machines API does not meter egress at all,
    so the figure can only come from a billing export or an offline invoice.
    Keeping it separate is also what lets the whole criterion be proven with a
    fake — the honest shipped answer is "unavailable".
    """

    def egress_bytes(self) -> Meter:
        """Return bytes billed as egress, or an unavailable meter."""


class UnavailableEgressMeter:
    """The shipped egress boundary: honest about having no source.

    Satisfies :class:`EgressMeter`. Reporting ``UNAVAILABLE`` is not a stub
    standing in for work not yet done — it is the correct answer for the Fly
    Machines API, and reporting 0 instead would tell an operator their fleet
    transferred nothing.
    """

    def egress_bytes(self) -> Meter:
        """Report that no readable egress meter exists on this provider."""
        return Meter(None, MetricQuality.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class FleetTelemetrySnapshot:
    """One deterministic, content-free cost observation of the whole fleet.

    Every field is either a :class:`Meter` carrying its own quality, or a
    content-free surrogate list. ``observed_at`` is excluded from equality, as
    ``FleetReconciliationReport.observed_at`` is, so the clock cannot make two
    otherwise identical observations differ.

    ``unmetered_running`` is carried verbatim from the reconciler: a running
    Machine whose clock could not be read stays visible by name instead of
    being folded into ``running_machine_seconds``.
    """

    activated_allocations: Meter
    provisioned_volumes: Meter
    stopped_rootfs_gb: Meter
    running_machine_seconds: Meter
    running_machine_seconds_by_allocation: tuple[AllocationMeter, ...]
    volume_gb: Meter
    snapshot_bytes: Meter
    egress_bytes: Meter
    duplicate_allocation_attempts: Meter
    refused_allocation_attempts: Meter
    orphan_provider_resources: Meter
    unconfirmed_deletions: Meter
    unmetered_running: tuple[str, ...]
    inventory_complete: bool
    observed_at: datetime = field(compare=False)


@dataclass(frozen=True, slots=True)
class FleetPriceTable:
    """Operator-supplied budget and unit prices; not one of them has a default.

    ADR-0013 Decision 4 names the reference figures as injected assumptions
    about a third party's price list. A default here would be exactly the
    business logic it forbids: it would let a cost appear without anyone
    having agreed to the number behind it, and it would go stale silently.
    ``hours_per_month`` and ``bytes_per_gb`` are injected for the same reason —
    a month is not a fixed number of hours and a "GB" is ``10**9`` or ``2**30``
    depending on whose invoice is being reconciled.
    """

    monthly_budget: Decimal
    volume_gb_month: Decimal
    stopped_rootfs_gb_month: Decimal
    running_machine_hour: Decimal
    egress_gb: Decimal
    snapshot_gb_month: Decimal
    hours_per_month: Decimal
    bytes_per_gb: int

    def __post_init__(self) -> None:
        """Reject a table that could only produce a meaningless estimate."""
        prices = (
            self.volume_gb_month,
            self.stopped_rootfs_gb_month,
            self.running_machine_hour,
            self.egress_gb,
            self.snapshot_gb_month,
        )
        if any(price < 0 for price in prices):
            raise ValueError("unit prices must not be negative")
        if self.monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive")
        if self.hours_per_month <= 0:
            raise ValueError("hours_per_month must be positive")
        if self.bytes_per_gb <= 0:
            raise ValueError("bytes_per_gb must be positive")


@dataclass(frozen=True, slots=True)
class BillingPeriodUsage:
    """Usage over one whole billing period, which an instant cannot supply.

    Deliberately not derived from :class:`FleetTelemetrySnapshot`: a single
    pass yields point-in-time capacity and a lower-bound duration, never the
    running fraction of a month, so wiring the two together would fabricate a
    monthly total out of an instant.
    """

    volume_gb: Decimal
    rootfs_gb: Decimal
    running_hours: Decimal
    egress_gb: Decimal
    snapshot_gb: Decimal

    def __post_init__(self) -> None:
        """Reject negative usage, which no meter can legitimately report."""
        quantities = (
            self.volume_gb,
            self.rootfs_gb,
            self.running_hours,
            self.egress_gb,
            self.snapshot_gb,
        )
        if any(quantity < 0 for quantity in quantities):
            raise ValueError("billing period usage must not be negative")


def estimate_monthly_cost(
    usage: BillingPeriodUsage,
    prices: FleetPriceTable,
) -> Decimal:
    """Return one allocation's estimated monthly cost from injected inputs.

    Pure, and ``Decimal`` throughout rather than ``float``: the figures are
    money reconciled against an invoice, and binary floating point cannot
    represent a cent exactly. ``stopped_fraction`` is derived here rather than
    passed in, so a caller cannot supply a running duration and a stopped
    fraction that contradict each other, and it is clamped at zero for a
    period whose running hours exceed the injected month.
    """
    stopped_fraction = max(
        Decimal(0),
        Decimal(1) - usage.running_hours / prices.hours_per_month,
    )
    return (
        usage.volume_gb * prices.volume_gb_month
        + usage.rootfs_gb * prices.stopped_rootfs_gb_month * stopped_fraction
        + usage.running_hours * prices.running_machine_hour
        + usage.egress_gb * prices.egress_gb
        + usage.snapshot_gb * prices.snapshot_gb_month
    )


def storage_bytes(
    snapshot: FleetTelemetrySnapshot,
    prices: FleetPriceTable,
) -> Meter:
    """Roll volume GB and snapshot bytes into one figure, in bytes.

    :meth:`FleetTelemetry.observe` never performs this conversion. Volumes are
    billed per GB-month and snapshots per byte; adding them inline would need a
    conversion base picked in the middle of a sum, and ``10**9`` versus
    ``2**30`` is an assumption about whose invoice is being reconciled. It
    therefore lives here, taken from the injected
    :attr:`FleetPriceTable.bytes_per_gb`, and the result is never cleaner than
    the weakest meter that fed it.
    """
    volumes = snapshot.volume_gb
    snapshots = snapshot.snapshot_bytes
    rolled = _measured(
        [
            None if volumes.value is None else volumes.value * prices.bytes_per_gb,
            snapshots.value,
        ],
        complete=True,
    )
    degraded = MetricQuality.ESTIMATED in (volumes.quality, snapshots.quality)
    if rolled.value is not None and degraded:
        return Meter(rolled.value, MetricQuality.ESTIMATED)
    return rolled


def _measured(values: Sequence[int | None], *, complete: bool) -> Meter:
    """Combine per-resource contributions into one quality-carrying figure.

    Exactly five arms, applied in this order:

    1. contributors exist and **none** is readable -> ``UNAVAILABLE``. This is
       the arm that matters. ``config.rootfs`` is a key Creek writes and reads
       back rather than a documented Fly response field, so "every Machine
       enumerated, none of them measurable" is an expected production case —
       and summing it to 0 would report a fleet of billing root filesystems as
       costing nothing.
    2. nothing observed at all and the enumeration did not complete ->
       ``UNAVAILABLE``; a rate-limited read is not an empty fleet.
    3. some but not all readable, or the enumeration did not complete ->
       ``ESTIMATED``, and the value is a **lower bound**.
    4. every contributor readable and the enumeration complete -> ``EXACT``.
    5. zero contributors and the enumeration complete -> ``EXACT`` 0, which is
       the only legitimate zero this module can produce.
    """
    readable = [value for value in values if value is not None]
    if values and not readable:
        return Meter(None, MetricQuality.UNAVAILABLE)
    if not values and not complete:
        return Meter(None, MetricQuality.UNAVAILABLE)
    if len(readable) < len(values) or not complete:
        return Meter(sum(readable), MetricQuality.ESTIMATED)
    return Meter(sum(readable), MetricQuality.EXACT)


def _lower_bound(meter: Meter) -> Meter:
    """Downgrade a readable figure to the lower bound it actually is."""
    if meter.value is None:
        return meter
    return Meter(meter.value, MetricQuality.ESTIMATED)


def _elapsed_seconds(resource: ProviderResource, now: datetime) -> int | None:
    """Return seconds since the resource's last provider-side write, if any."""
    last_modified = resource.last_modified_at
    if last_modified is None:
        return None
    return max(0, int((now - last_modified).total_seconds()))


def _of_class(
    inventory: InventorySnapshot,
    resource_class: ProviderResourceClass,
) -> list[ProviderResource]:
    """Return every observed resource of one billable kind."""
    return [
        resource
        for resource in inventory.resources
        if resource.resource_class is resource_class
    ]


def _divergences(
    report: FleetReconciliationReport,
    kind: DivergenceKind,
) -> list[int]:
    """Return one contribution per divergence of *kind*, for counting."""
    return [1 for divergence in report.divergences if divergence.kind is kind]


class _ReplayInventory:
    """Serve one already-read snapshot, so a pass reads the provider once.

    Satisfies :class:`~creek_mcp.provisioning.inventory.ProviderInventory`
    without touching the network. Composing the reconciler over this adapter
    is what lets the capacity half and the divergence half describe the same
    instant: calling the provider a second time would double the wire load and
    let the two halves disagree. Teaching ``reconcile()`` to accept a pre-read
    snapshot instead would add an import to a module whose import set is
    pinned by exact equality.
    """

    def __init__(self, snapshot: InventorySnapshot) -> None:
        """Hold the frozen snapshot this pass already read."""
        self._snapshot = snapshot

    def list_resources(self) -> InventorySnapshot:
        """Return the snapshot verbatim, issuing no provider request."""
        return self._snapshot


class FleetTelemetry:
    """Observe what the provider is billing for, and only observe it."""

    def __init__(
        self,
        store: ProvisioningStore,
        inventory: ProviderInventory,
        policy: FleetReconcilePolicy,
        *,
        egress: EgressMeter,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind read-only store, inventory and egress boundaries plus a clock."""
        self._store = store
        self._inventory = inventory
        self._policy = policy
        self._egress = egress
        self._clock = clock

    def observe(self) -> FleetTelemetrySnapshot:
        """Return one deterministic cost observation, having mutated nothing.

        The provider is read exactly once; the reconciler runs over that same
        frozen snapshot. Nothing durable is written, so two passes over
        unchanged state compare equal and a crash mid-pass leaves no residue.
        """
        now = self._clock()
        inventory = self._snapshot()
        allocations = self._store.live_allocations()
        live = {allocation.provider_allocation_id for allocation in allocations}
        report = FleetReconciler(
            self._store,
            _ReplayInventory(inventory),
            self._policy,
            clock=lambda: now,
        ).reconcile()
        fleet_seconds, by_allocation = self._running_seconds(inventory, live, now=now)
        return FleetTelemetrySnapshot(
            activated_allocations=Meter(len(allocations), MetricQuality.EXACT),
            provisioned_volumes=self._provisioned_volumes(inventory),
            stopped_rootfs_gb=self._stopped_rootfs_gb(inventory),
            running_machine_seconds=fleet_seconds,
            running_machine_seconds_by_allocation=by_allocation,
            volume_gb=self._volume_gb(inventory),
            snapshot_bytes=self._snapshot_bytes(inventory),
            egress_bytes=self._egress_bytes(),
            duplicate_allocation_attempts=Meter(
                self._store.duplicate_activation_attempts(),
                MetricQuality.EXACT,
            ),
            refused_allocation_attempts=self._refused_attempts(),
            orphan_provider_resources=_measured(
                _divergences(report, DivergenceKind.ORPHAN_PROVIDER_RESOURCE),
                complete=inventory.complete,
            ),
            unconfirmed_deletions=Meter(
                len(_divergences(report, DivergenceKind.DELETION_UNCONFIRMED)),
                MetricQuality.EXACT,
            ),
            unmetered_running=report.unmetered_running,
            inventory_complete=inventory.complete,
            observed_at=now,
        )

    def _snapshot(self) -> InventorySnapshot:
        """Read the inventory once, keeping whatever a partial pass observed.

        A boundary that raises rather than reporting partiality is still
        handled, because ``ProviderInventory`` is an injected Protocol and a
        third-party implementation may do so — but nothing is inferred from
        the resulting emptiness beyond ``complete`` being False.
        """
        try:
            return self._inventory.list_resources()
        except ProviderError:
            return InventorySnapshot(resources=(), complete=False)

    def _egress_bytes(self) -> Meter:
        """Read the injected egress boundary, tolerating one that raises."""
        try:
            return self._egress.egress_bytes()
        except ProviderError:
            return Meter(None, MetricQuality.UNAVAILABLE)

    @staticmethod
    def _refused_attempts() -> Meter:
        """Report allocation attempts the store refused as unreadable.

        ``ActivationConflictError`` and the per-consumer alias cap reject an
        attempt *before* anything durable is written, so no query can count
        them. Shipping them as a permanently unavailable meter rather than
        folding them into ``duplicate_allocation_attempts`` is the point: a
        zero there would be invisible in a green run and would invite a reader
        to take the duplicate count for the whole truth.
        """
        return Meter(None, MetricQuality.UNAVAILABLE)

    @staticmethod
    def _provisioned_volumes(inventory: InventorySnapshot) -> Meter:
        """Count the volumes the provider is billing for."""
        return _measured(
            [1 for _ in _of_class(inventory, ProviderResourceClass.VOLUME)],
            complete=inventory.complete,
        )

    @staticmethod
    def _volume_gb(inventory: InventorySnapshot) -> Meter:
        """Total persistent volume capacity, in GB, as the provider reports it."""
        return _measured(
            [
                resource.size_gb
                for resource in _of_class(inventory, ProviderResourceClass.VOLUME)
            ],
            complete=inventory.complete,
        )

    @staticmethod
    def _snapshot_bytes(inventory: InventorySnapshot) -> Meter:
        """Total snapshot bytes, reported separately because Fly bills them so."""
        return _measured(
            [
                resource.size_bytes
                for resource in _of_class(inventory, ProviderResourceClass.SNAPSHOT)
            ],
            complete=inventory.complete,
        )

    @staticmethod
    def _stopped_rootfs_gb(inventory: InventorySnapshot) -> Meter:
        """Root filesystem capacity on Machines that are not billing for CPU."""
        return _measured(
            [
                resource.size_gb
                for resource in _of_class(inventory, ProviderResourceClass.MACHINE)
                if resource.state not in _RUNNING_STATES
            ],
            complete=inventory.complete,
        )

    @staticmethod
    def _running_seconds(
        inventory: InventorySnapshot,
        live: set[str],
        *,
        now: datetime,
    ) -> tuple[Meter, tuple[AllocationMeter, ...]]:
        """Return the fleet-wide and per-allocation running lower bounds.

        Both figures are ``ESTIMATED`` whenever they exist, including when
        they are 0: ``last_modified_at`` is Fly's ``updated_at``, which any
        provider-side write resets, so the duration can only ever under-report.
        The quality follows the meter's semantics rather than its data — a
        quality that flipped to ``EXACT`` on a zero would claim certainty about
        a Machine that may simply have started since the last write.
        """
        grouped: defaultdict[str, list[int | None]] = defaultdict(list)
        for resource in _of_class(inventory, ProviderResourceClass.MACHINE):
            surrogate = resource.provider_allocation_id
            if surrogate is None or surrogate not in live:
                continue
            if resource.state not in _RUNNING_STATES:
                continue
            grouped[surrogate].append(_elapsed_seconds(resource, now))
        fleet = _lower_bound(
            _measured(
                [value for values in grouped.values() for value in values],
                complete=inventory.complete,
            )
        )
        return fleet, tuple(
            AllocationMeter(
                subject=surrogate,
                meter=_lower_bound(_measured(values, complete=inventory.complete)),
            )
            for surrogate, values in sorted(grouped.items())
        )
