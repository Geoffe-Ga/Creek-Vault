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
``start``, ``stop``, ``delete`` or repair path anywhere here. PR2 measures and
nothing below compares a meter to :attr:`FleetPriceTable.monthly_budget`;
``creek_mcp.provisioning.alerts`` is where a threshold is evaluated, and it is
report-only too — an alarm notifies, it never remediates.

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

Three known gaps, stated so they are not silent ones.

1. A pass reads the store once per query but is **not one transaction**: three
   separate read-only queries run on three connections alongside the provider
   enumeration, so fields derived from different queries can describe different
   instants. :meth:`FleetTelemetry.observe` names which field each query feeds.
2. ``EXACT`` means exact over Creek's *enumeration scope* — apps named
   ``<app_prefix>-*`` in one organization — never exact over what the provider
   bills. A Creek resource outside that scope is invisible to every meter here.
3. Orphan resources, duplicate provider resources and unconfirmed deletions are
   counted off the reconciler's own ``divergences`` rather than re-derived, so
   nothing downstream can disagree with the reconciler about what one is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

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
from creek_mcp.provisioning.store import ProvisioningStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime, timedelta

    from creek_mcp.provisioning.inventory import ProviderInventory, ProviderResource
    from creek_mcp.provisioning.models import OperatorAllocationView
    from creek_mcp.provisioning.reconcile import (
        FleetReconcilePolicy,
        FleetReconciliationReport,
    )

_STOPPED_STATES: Final[frozenset[str]] = frozenset({"created", "stopped", "suspended"})
"""Fly Machine states known to hold a root filesystem without billing for CPU.

A **closed positive set**, deliberately not the complement of
:data:`~creek_mcp.provisioning.reconcile._RUNNING_STATES`. Negating the running
set classes every state Creek does not know — ``stopping``, ``replacing``, and
whatever Fly adds next — as "stopped, and therefore billing this much rootfs",
which is a confident claim about a Machine nobody classified. Note in
particular that ``unknown``, the placeholder ``FlyProviderDriver`` records when
Fly reports no state at all, is absent from both sets by design.
"""


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

    **What EXACT means here.** ``FlyProviderDriver.list_resources`` enumerates
    apps named ``<app_prefix>-*`` inside one organization. So ``EXACT`` means
    "exact over the resources Creek's own enumeration scope can see", never
    "exact over what the provider bills". A Creek-owned resource in another
    organization, or under an app whose name lost the prefix, is outside every
    meter below and no quality on this record will say so. Reconciling an
    invoice means comparing against that scope, not against the account.

    **Which meters are fleet-wide and which are live-fenced.** Every capacity
    and duration meter is *fleet-wide*: it counts orphaned resources too,
    because the operator is billed for them. ``unmetered_running`` is the one
    field carried verbatim from the reconciler and is therefore *live-fenced* —
    it names only live allocations. A running orphan whose clock is unreadable
    is consequently absent from it, but it is not lost: it contributes an
    unreadable value to ``running_machine_seconds``, which degrades per the
    combinator rather than absorbing it as a zero.

    ``unclassified_machines`` names Machines in a state that is neither known-
    running nor known-stopped. They contribute an unreadable value to both the
    stopped-capacity and running-duration meters rather than being silently
    assigned to either.

    **Why the whole reconciliation report is carried.** Every divergence meter
    below reduces its divergences to a *count*, which is all a cost report
    needs. An alarm needs the **subjects**, and the only other way to obtain
    them is to run :class:`FleetReconciler` a second time over a second
    provider read and a second store read — reintroducing exactly the
    split-instant defect :class:`_ReplayInventory` and :class:`_ReplayStore`
    exist to close, and letting the alarm disagree with the meter beside it
    about what one pass saw. So the report this pass already built is carried
    verbatim. It is content-free by construction (``reconcile.py`` names only
    surrogates) and its own ``observed_at`` is likewise excluded from equality,
    so two passes over unchanged state still compare equal.
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
    duplicate_provider_resources: Meter
    refused_allocation_attempts: Meter
    orphan_provider_resources: Meter
    unconfirmed_deletions: Meter
    unmetered_running: tuple[str, ...]
    unclassified_machines: tuple[str, ...]
    inventory_complete: bool
    reconciliation: FleetReconciliationReport
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
    """Return one usage record's estimated monthly cost from injected inputs.

    Pure: the scope of the answer is whatever scope *usage* covers, and that is
    fixed entirely by the caller. One allocation's usage yields one
    allocation's cost; a whole billing period's fleet usage yields the fleet's,
    which is the scope ADR-0013 Decision 4's "operator-set monthly budget" is
    written in and the scope ``alerts.py`` calls this at.

    ``Decimal`` throughout rather than ``float``: the figures are
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


def _stopped_rootfs_contribution(resource: ProviderResource) -> int | None:
    """Return a stopped Machine's rootfs GB, or None when its state is unknown."""
    if resource.state not in _STOPPED_STATES:
        return None
    return resource.size_gb


def _unclassified_machines(inventory: InventorySnapshot) -> tuple[str, ...]:
    """Name Machines in a state that is neither known-running nor known-stopped.

    Mirrors :attr:`FleetReconciliationReport.unmetered_running`: a resource
    Creek could not classify is surfaced by surrogate rather than folded into
    whichever meter happens to have a negated predicate. Only the surrogate is
    carried — the provider's state string is free text Creek does not control,
    and this record stays content-free by construction.
    """
    return tuple(
        sorted(
            {
                resource.provider_allocation_id
                for resource in _of_class(inventory, ProviderResourceClass.MACHINE)
                if resource.provider_allocation_id is not None
                and resource.state not in _RUNNING_STATES
                and resource.state not in _STOPPED_STATES
            }
        )
    )


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


class _ReplayStore(ProvisioningStore):
    """Serve one already-read operator view, so a pass reads the store once.

    The provider side already had :class:`_ReplayInventory`; this is the
    durable side of the same problem, and it is not cosmetic.
    ``ProvisioningStore._connect`` opens a fresh autocommit connection per
    call and ``ProvisioningWorker`` is a concurrent writer by design, so two
    reads inside one pass genuinely observe two committed states. A provision
    settling between them yields a snapshot in which the same Machine is
    attributed to a live allocation by the duration meter and to nobody by the
    divergence meter — an internally contradictory report an operator acts on.

    It subclasses rather than duck-types because ``ProvisioningStore`` is a
    concrete class, not a Protocol, and ``FleetReconciler`` is typed on it.
    :meth:`ProvisioningStore.__init__` is deliberately **not** called: the
    adapter opens no database and binds no path, so every inherited mutator
    raises :class:`AttributeError` rather than reaching SQLite. Report-only is
    therefore a property of the object handed to the reconciler, not only of
    the two methods it happens to call.
    """

    def __init__(
        self,
        allocations: list[OperatorAllocationView],
        unconfirmed: list[OperatorAllocationView],
    ) -> None:
        """Hold the two operator views this pass already read."""
        self._allocations = allocations
        self._unconfirmed = unconfirmed

    def live_allocations(self) -> list[OperatorAllocationView]:
        """Return the live view verbatim, issuing no query."""
        return list(self._allocations)

    def unconfirmed_deletions(
        self,
        older_than: timedelta,
        *,
        now: datetime | None = None,
    ) -> list[OperatorAllocationView]:
        """Return the stale-deletion view verbatim, issuing no query.

        The window and the instant are ignored because the caller already
        applied both when it performed the single real read.
        """
        del older_than, now
        return list(self._unconfirmed)


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

        The provider is read exactly once and the reconciler runs over that
        same frozen snapshot; the durable store is likewise read exactly once
        per query, and the reconciler runs over :class:`_ReplayStore` rather
        than querying again. Nothing durable is written, so two passes over
        unchanged state compare equal and a crash mid-pass leaves no residue.

        **Disclosed inconsistency window.** Even so, a pass is not one
        transaction. It issues three separate read-only queries —
        ``live_allocations`` (feeding ``activated_allocations`` and every
        divergence meter), ``unconfirmed_deletions`` (feeding
        ``unconfirmed_deletions``) and ``duplicate_activation_attempts``
        (feeding ``duplicate_allocation_attempts``) — each on its own
        connection, alongside the one provider enumeration. A job settling
        between any two of them makes those two fields describe different
        instants. Closing that would require a transaction the store does not
        expose to a reader; what is closed here is the far worse case, where
        one query ran *twice* and two fields derived from the same query
        disagreed inside a single snapshot.
        """
        now = self._clock()
        inventory = self._snapshot()
        allocations = self._store.live_allocations()
        report = FleetReconciler(
            _ReplayStore(
                allocations,
                self._store.unconfirmed_deletions(
                    self._policy.unconfirmed_deletion_after,
                    now=now,
                ),
            ),
            _ReplayInventory(inventory),
            self._policy,
            clock=lambda: now,
        ).reconcile()
        fleet_seconds, by_allocation = self._running_seconds(inventory, now=now)
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
            duplicate_provider_resources=_measured(
                _divergences(report, DivergenceKind.DUPLICATE_ALLOCATION),
                complete=inventory.complete,
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
            unclassified_machines=_unclassified_machines(inventory),
            inventory_complete=inventory.complete,
            reconciliation=report,
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
        """Root filesystem capacity on Machines known to be stopped.

        Never better than ``ESTIMATED``, even when every contributor was
        readable and the enumeration completed. ``config.rootfs.size_gb`` is
        Creek's *own request*, echoed back by the Machines listing — it is not
        a documented Fly response field and it is not the provider's statement
        of what it is billing. Reporting a request as ``EXACT`` would claim
        provider confirmation this module never obtained, which is the same
        defect as reporting an assumption as an observation.

        A Machine whose state is in neither the running nor the stopped set
        contributes an unreadable value rather than being assumed stopped, so
        the meter degrades instead of asserting a capacity for it.
        """
        return _lower_bound(
            _measured(
                [
                    _stopped_rootfs_contribution(resource)
                    for resource in _of_class(inventory, ProviderResourceClass.MACHINE)
                    if resource.state not in _RUNNING_STATES
                ],
                complete=inventory.complete,
            )
        )

    @staticmethod
    def _running_seconds(
        inventory: InventorySnapshot,
        *,
        now: datetime,
    ) -> tuple[Meter, tuple[AllocationMeter, ...]]:
        """Return the fleet-wide and per-allocation running lower bounds.

        **Fleet-wide, deliberately not live-fenced.** Every capacity meter on
        the snapshot counts orphaned resources, because the operator is billed
        for them; fencing the *duration* meter to live allocations while
        leaving capacity fleet-wide made an orphan burning CPU for a month
        indistinguishable from a healthy idle fleet — both reported
        ``Meter(0, ESTIMATED)`` with an empty per-allocation tuple. An orphan
        already carries a surrogate, so it is attributed by that surrogate,
        and the reconciler reports it as an orphan in the same pass.

        Both figures are ``ESTIMATED`` whenever they exist, including when
        they are 0: ``last_modified_at`` is Fly's ``updated_at``, which any
        provider-side write resets, so the duration can only ever
        under-report. The quality follows the meter's semantics rather than
        its data — a quality that flipped to ``EXACT`` on a zero would claim
        certainty about a Machine that may simply have started since the last
        write.

        A Machine whose state is in neither the running nor the stopped set
        contributes an unreadable value, because nothing here knows whether it
        is billing for CPU; it is named in ``unclassified_machines``.
        """
        grouped: defaultdict[str, list[int | None]] = defaultdict(list)
        for resource in _of_class(inventory, ProviderResourceClass.MACHINE):
            surrogate = resource.provider_allocation_id
            if surrogate is None or resource.state in _STOPPED_STATES:
                continue
            grouped[surrogate].append(
                _elapsed_seconds(resource, now)
                if resource.state in _RUNNING_STATES
                else None
            )
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
