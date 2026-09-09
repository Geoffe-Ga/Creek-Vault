"""Report-only fleet alarms over one telemetry pass (#1769).

ADR-0013 Decision 4 requires the control plane to raise an alarm on departure
from an operator-set monthly budget, and Decision 6 puts reconciliation and
alerts on the same footing — a divergence "remains visible to reconciliation
and alerts until the provider confirms". PR1 shipped the report-only
reconciler and PR2 the cost telemetry; this module is the half that compares an
observation to a threshold and says so.

*Report only, restated for alarms.* An alarm **notifies**; it never remediates.
:class:`FleetAlarms` is typed on
:class:`~creek_mcp.provisioning.telemetry.FleetTelemetry`, which is itself typed
on ``ProviderInventory`` and cannot mutate anything, and
:class:`AlertSink` declares exactly one method, so a sink is never handed a
repair capability. Nothing here holds a ``ProvisioningStore`` or a
``ProviderDriver``; the whole pass puts nothing but GETs on the wire.

*Subjects come from the single pass, not from a second read.* Alerts are keyed
by ``(code, subject)``, and a subject exists only on a
:class:`~creek_mcp.provisioning.reconcile.FleetReconciliationReport`. Running
:class:`~creek_mcp.provisioning.reconcile.FleetReconciler` again here would
mean a second provider read and a second store read, so the report is taken off
:attr:`~creek_mcp.provisioning.telemetry.FleetTelemetrySnapshot.reconciliation`
instead. Counts likewise run through telemetry's own ``_measured``, so an
alert and the meter beside it are arithmetically incapable of disagreeing.

*No silent non-alert.* This is the inverse of the defect
:class:`~creek_mcp.provisioning.inventory.MetricQuality` exists to prevent, and
it is the rule this module is built around: **an alarm whose threshold input
was UNAVAILABLE emits an explicit unevaluable alert — never silence, and never
a compliant reading.** Every alert carries a
:class:`~creek_mcp.provisioning.telemetry.Meter`, so an alarm that could not be
evaluated is distinguishable from one that evaluated and found nothing. Three
places make that structural rather than aspirational:

1. ``FleetReconciler._is_running_beyond`` refuses to report a Machine with no
   readable clock, so such a Machine yields *no* divergence and appears only in
   ``unmetered_running``. It raises :attr:`AlertCode.CONTINUOUS_RUNNING`
   carrying ``Meter(None, UNAVAILABLE)`` here.
2. A live Machine in ``unclassified_machines`` — a state neither known-running
   nor known-stopped — does the same.
3. On an incomplete enumeration ``FleetReconciler._missing`` suppresses itself
   *wholesale*, so ``missing_resource`` has no per-subject signal at all and an
   absence of findings would be a confident zero. One fleet-scoped unevaluable
   alert is emitted for each inventory-derived code instead.

:meth:`Alert.__post_init__` closes the gap ``Meter``'s own biconditional
leaves: ``Meter(0, EXACT)`` satisfies that biconditional and is exactly a
raised alarm with nothing behind it, so it is refused.

*Dedupe is per-run and stateless.* One adopted orphan produces one divergence
per resource and one alert per ``(code, subject)``, carrying the contributor
count. Nothing durable is written — no table, no schema version, no
suppression ledger — because a durable ledger would break the equal-passes
purity property both PR1 and PR2 hold and test: the second pass over unchanged
state would emit nothing. **Cross-run suppression is the injected sink's
contract**, exactly as ``FakeKeyReleaseSink`` owns key-release idempotency, and
``observed_at`` is carried (excluded from equality) so a sink can implement a
time-boxed policy.

*The budget is injected, and it is fleet-scoped.* A billing period cannot be
derived from an instant — ``telemetry.py`` rules that out in terms — so it
arrives through a third read-only Protocol, :class:`BillingPeriodSource`, whose
shipped implementation answers ``UNAVAILABLE`` honestly. Its alert's subject is
``None``: ADR-0013 Decision 4 says "an operator-set monthly budget" and
Decision 7 "the approved fleet budget", and a typed ``None`` cannot collide
with a ``fly-<24 hex>`` surrogate by construction rather than by a pinned test.

Four judgement calls, stated so they are known gaps rather than silent ones.

1. A departure raised here is a **single-period** departure. It does *not*
   satisfy ADR-0013 Decision 7's "three rolling months above the approved fleet
   budget" trigger: a rolling window needs persisted samples, which is a schema
   change this slice deliberately does not take.
2. A running orphan past the policy window is a **named non-alert**. It is
   covered by :attr:`AlertCode.ORPHAN_RESOURCE`; ``continuous_running`` is out
   of scope for non-live subjects because ``FleetReconciler._running`` is
   live-fenced (PR1 judgement call 3). Re-deriving the threshold here would
   duplicate one the reconciler owns and let the alarm disagree with the meter.
   If that is ever to be closed, the fix belongs in ``FleetReconciler``.
3. ``missing_resource`` inherits reconcile.py's **app granularity**: a volume
   that vanishes under a still-listed app is invisible to this alarm too.
4. ``refused_allocation_attempts`` and ``egress_bytes`` are permanently
   unavailable meters by construction rather than by circumstance, so they are
   deliberately not alarmed — an alarm that could never be evaluated on any
   pass is noise, not a signal.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum, unique
from threading import Lock
from typing import TYPE_CHECKING, Final, Protocol

from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.inventory import MetricQuality
from creek_mcp.provisioning.reconcile import DivergenceKind
from creek_mcp.provisioning.telemetry import (
    Meter,
    _measured,
    estimate_monthly_cost,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from datetime import datetime
    from decimal import Decimal

    from creek_mcp.provisioning.telemetry import (
        BillingPeriodUsage,
        FleetPriceTable,
        FleetTelemetry,
        FleetTelemetrySnapshot,
    )


@unique
class AlertCode(StrEnum):
    """The closed set of conditions one alarm pass can raise.

    Six members for five :class:`~creek_mcp.provisioning.reconcile.DivergenceKind`
    members plus the budget. Every divergence kind maps to one of these and
    ``_DIVERGENCE_ALERTS`` is asserted total, so a sixth kind fails loudly
    rather than escaping the alarm surface — which is the silent-non-alert
    defect at module granularity.
    """

    DUPLICATE_ALLOCATION = "duplicate_allocation"
    ORPHAN_RESOURCE = "orphan_resource"
    MISSING_RESOURCE = "missing_resource"
    STUCK_DELETION = "stuck_deletion"
    CONTINUOUS_RUNNING = "continuous_running"
    BUDGET_DEPARTURE = "budget_departure"


class FleetAlarmError(RuntimeError):
    """A divergence fell outside the closed alarm classification."""


_DIVERGENCE_ALERTS: Final[Mapping[DivergenceKind, AlertCode]] = {
    DivergenceKind.ORPHAN_PROVIDER_RESOURCE: AlertCode.ORPHAN_RESOURCE,
    DivergenceKind.MISSING_PROVIDER_RESOURCE: AlertCode.MISSING_RESOURCE,
    DivergenceKind.DUPLICATE_ALLOCATION: AlertCode.DUPLICATE_ALLOCATION,
    DivergenceKind.DELETION_UNCONFIRMED: AlertCode.STUCK_DELETION,
    DivergenceKind.RUNNING_BEYOND_POLICY: AlertCode.CONTINUOUS_RUNNING,
}
"""Every divergence kind's alarm code. Totality is asserted, not assumed."""

_INVENTORY_DERIVED: Final[frozenset[AlertCode]] = frozenset(
    {
        AlertCode.DUPLICATE_ALLOCATION,
        AlertCode.ORPHAN_RESOURCE,
        AlertCode.MISSING_RESOURCE,
        AlertCode.CONTINUOUS_RUNNING,
    }
)
"""The codes whose evidence is the provider enumeration, and so can go partial.

``stuck_deletion`` is store-sourced and ``budget_departure`` comes from the
injected billing boundary, so neither degrades when a provider read does.
"""


def _code_for(kind: DivergenceKind) -> AlertCode:
    """Return the alarm code for *kind*, refusing to drop an unmapped one.

    Raising rather than skipping is the point: a divergence kind with no code
    would be a whole class of finding that never reaches an operator, which is
    the silent non-alert one level up from a single unevaluated threshold.
    """
    try:
        return _DIVERGENCE_ALERTS[kind]
    except KeyError as error:
        raise FleetAlarmError("divergence kind is unalarmed") from error


@dataclass(frozen=True, slots=True)
class Alert:
    """One content-free alarm naming a surrogate, or the fleet, and its evidence.

    ``subject`` is ``None`` for a fleet-scoped alarm. A typed sentinel rather
    than a reserved string: it cannot collide with a ``fly-<24 hex>`` surrogate
    by construction, and mypy strict forces every reader to handle it.

    ``contributing`` says how far the alarm can be trusted. ``UNAVAILABLE``
    means *this alarm could not be evaluated* — never that it was evaluated and
    found compliant — and a compliant evaluation raises no alert at all, so the
    two are never confusable. ``observed_at`` is excluded from equality, as
    ``FleetReconciliationReport.observed_at`` is, so the clock cannot make two
    otherwise identical passes differ; it is carried rather than dropped
    because the sink owns cross-run suppression and any time-boxed policy needs
    an instant.
    """

    code: AlertCode
    subject: str | None
    contributing: Meter
    observed_at: datetime = field(compare=False)

    def __post_init__(self) -> None:
        """Refuse a raised alarm with nothing behind it.

        ``Meter``'s own biconditional ties an absent value to ``UNAVAILABLE``,
        but ``Meter(0, EXACT)`` satisfies it perfectly and ``_measured([],
        complete=True)`` returns exactly that. An alert carrying it asserts a
        verdict its input could not support, so it is unconstructable here
        rather than merely discouraged.
        """
        if self.contributing.value == 0:
            raise ValueError("a raised alert must name at least one contributor")


@dataclass(frozen=True, slots=True)
class BillingPeriodReading:
    """One billing period as a source reported it, or an honest unavailability.

    Carries the same biconditional :class:`Meter` does, for the same reason: a
    period nobody could read must not be indistinguishable from a period read
    and found empty.
    """

    usage: BillingPeriodUsage | None
    quality: MetricQuality

    def __post_init__(self) -> None:
        """Refuse a usage and a quality that disagree about being readable."""
        if (self.usage is None) != (self.quality is MetricQuality.UNAVAILABLE):
            raise ValueError("a reading has usage if and only if it is not unavailable")


class BillingPeriodSource(Protocol):
    """A read-only boundary reporting one whole billing period's usage.

    Declared as its own capability, shaped exactly like
    :class:`~creek_mcp.provisioning.telemetry.EgressMeter`, because a billing
    period cannot be derived from a telemetry pass at all: a pass yields
    point-in-time capacity and a lower-bound instantaneous duration, and
    feeding either into a monthly figure would fabricate it. The period can
    only come from an invoice or a billing export.
    """

    def billing_period_usage(self) -> BillingPeriodReading:
        """Return the period's usage, or an explicit unavailability."""


class UnavailableBillingPeriodSource:
    """The shipped billing boundary: honest about having no source.

    Satisfies :class:`BillingPeriodSource`. Reporting ``UNAVAILABLE`` is not a
    stub standing in for work not yet done — it is the correct answer for the
    Fly Machines API, which exposes apps, volumes and Machines and no billing
    surface whatever. Reporting a zero-usage period instead would tell an
    operator their fleet is comfortably inside budget on no evidence at all.
    """

    def billing_period_usage(self) -> BillingPeriodReading:
        """Report that no readable billing period exists on this provider."""
        return BillingPeriodReading(None, MetricQuality.UNAVAILABLE)


class AlertSink(Protocol):
    """Deliver one alarm onward. Exactly one method, and it returns nothing.

    The narrowness is the design: a sink that could be handed a repair
    capability is a remediation path reachable from an alarm, and #1769 is
    report-only throughout.
    """

    def deliver(self, alert: Alert) -> None:
        """Deliver one alert, or acknowledge an identical prior delivery."""


class FakeAlertSink:
    """Secret-free idempotent alarm sink for contract tests.

    Holds the cross-run suppression this module deliberately does not: keyed by
    ``(code, subject, contributing)``, so a re-observation of an unchanged
    condition is delivered once.

    One deliberate divergence from ``FakeKeyReleaseSink``, which raises
    ``CeremonyConflictError`` on a conflicting replay: a repeat delivery here is
    an idempotent **no-op**. A changed contributor count between runs is normal
    for an alarm — an orphan gains a resource, a duplicate is cleaned up — so
    raising would turn a legitimate re-observation into an error.
    """

    def __init__(self) -> None:
        """Initialize an empty ordered ledger and its suppression counter."""
        self._lock = Lock()
        self._delivered: dict[tuple[AlertCode, str | None, Meter], Alert] = {}
        self._suppressed = 0

    @property
    def delivered(self) -> tuple[Alert, ...]:
        """Return the distinct alerts this sink has accepted, in order."""
        with self._lock:
            return tuple(self._delivered.values())

    @property
    def suppressed_count(self) -> int:
        """Return how many deliveries were recognised as repeats."""
        with self._lock:
            return self._suppressed

    def deliver(self, alert: Alert) -> None:
        """Accept one alert, or recognise it as an unchanged re-observation."""
        key = (alert.code, alert.subject, alert.contributing)
        with self._lock:
            if key in self._delivered:
                self._suppressed += 1
                return
            self._delivered[key] = alert


def _sort_key(alert: Alert) -> tuple[str, str]:
    """Return one total, content-free ordering so two passes compare equal.

    Total because ``(code, subject)`` is also the dedupe key, so it appears at
    most once per pass; a surrogate is never the empty string, so the
    fleet-scoped sentinel cannot tie with one.
    """
    return (alert.code.value, alert.subject or "")


def _orphaned_subjects(snapshot: FleetTelemetrySnapshot) -> set[str]:
    """Return the surrogates this pass reported as orphaned."""
    return {
        divergence.subject
        for divergence in snapshot.reconciliation.divergences
        if divergence.kind is DivergenceKind.ORPHAN_PROVIDER_RESOURCE
    }


def _live_unevaluable_running(snapshot: FleetTelemetrySnapshot) -> set[str]:
    """Return live subjects whose continuous-running threshold could not be read.

    Two sources, both then live-fenced to match the exact scope in which
    ``FleetReconciler._running`` evaluates ``RUNNING_BEYOND_POLICY``.
    ``unmetered_running`` is already live-fenced by the reconciler.
    ``unclassified_machines`` is computed fleet-wide, so the orphan subjects
    are subtracted — exact, because a surrogate absent from the live set yields
    an orphan divergence for every one of its resources.

    ``running_machine_seconds_by_allocation`` is deliberately **not** a third
    source. It is fleet-wide by design, so including it would raise an
    unevaluable ``continuous_running`` alert for a running orphan whose clock
    is unreadable, while a running orphan whose clock **is** readable and is
    months past policy would raise nothing at all — the worse condition silent
    and the lesser one alarming, with the alarm disagreeing with the reconciler
    about what is even in scope.
    """
    orphaned = _orphaned_subjects(snapshot)
    return set(snapshot.reconciliation.unmetered_running) | (
        set(snapshot.unclassified_machines) - orphaned
    )


class FleetAlarms:
    """Compare one telemetry pass against operator thresholds, and only report."""

    def __init__(
        self,
        telemetry: FleetTelemetry,
        *,
        prices: FleetPriceTable,
        billing: BillingPeriodSource,
        sink: AlertSink,
    ) -> None:
        """Bind one read-only observation, a budget, a billing period and a sink."""
        self._telemetry = telemetry
        self._prices = prices
        self._billing = billing
        self._sink = sink

    def raise_alerts(self) -> tuple[Alert, ...]:
        """Return every alarm one pass raises, having mutated nothing.

        Pure and sink-free: the provider and the store are read exactly once,
        by :meth:`FleetTelemetry.observe`, and nothing durable is written — so
        two passes over unchanged state return an equal tuple and a crash
        mid-pass leaves no residue.
        """
        snapshot = self._telemetry.observe()
        return tuple(
            sorted(
                [
                    *self._divergence_alerts(snapshot),
                    *_fleet_unevaluable(snapshot),
                    *self._budget_alerts(snapshot.observed_at),
                ],
                key=_sort_key,
            )
        )

    def notify(self) -> tuple[Alert, ...]:
        """Raise, then deliver each alert to the sink, then return them all.

        The tuple is computed *before* any delivery, so a sink that raises can
        neither truncate nor corrupt it; the exception propagates rather than
        being swallowed, because a sink that cannot deliver is itself an
        operator-visible failure.
        """
        raised = self.raise_alerts()
        for alert in raised:
            self._sink.deliver(alert)
        return raised

    @staticmethod
    def _divergence_alerts(snapshot: FleetTelemetrySnapshot) -> Iterator[Alert]:
        """Collapse divergences and unevaluable subjects to one alert per key.

        Contributions run through telemetry's own ``_measured``, so the alert
        count and the meter beside it are the same function and cannot
        disagree. A subject contributing an unreadable value degrades its
        alert's meter exactly as it degrades the snapshot's.
        """
        grouped: defaultdict[tuple[AlertCode, str | None], list[int | None]] = (
            defaultdict(list)
        )
        for divergence in snapshot.reconciliation.divergences:
            grouped[(_code_for(divergence.kind), divergence.subject)].append(1)
        for unevaluable in _live_unevaluable_running(snapshot):
            grouped[(AlertCode.CONTINUOUS_RUNNING, unevaluable)].append(None)
        for (code, subject), values in grouped.items():
            yield Alert(
                code,
                subject,
                _measured(
                    values,
                    complete=snapshot.inventory_complete
                    or code not in _INVENTORY_DERIVED,
                ),
                snapshot.observed_at,
            )

    def _budget_alerts(self, observed_at: datetime) -> Iterator[Alert]:
        """Compare an injected billing period against the operator's budget.

        Three outcomes, and the first is the one that matters: a period nobody
        could read raises an explicit unevaluable departure rather than
        silence. An evaluated period over budget raises a departure; an
        evaluated period at or under it raises nothing, because that is an
        alarm that *was* evaluated and found compliant.
        """
        reading = self._read_billing_period()
        if reading.usage is None:
            yield Alert(
                AlertCode.BUDGET_DEPARTURE,
                None,
                Meter(None, MetricQuality.UNAVAILABLE),
                observed_at,
            )
        elif self._departed(reading.usage):
            yield Alert(
                AlertCode.BUDGET_DEPARTURE,
                None,
                Meter(1, reading.quality),
                observed_at,
            )

    def _departed(self, usage: BillingPeriodUsage) -> bool:
        """Return whether *usage* costs strictly more than the operator's budget."""
        estimate: Decimal = estimate_monthly_cost(usage, self._prices)
        return estimate > self._prices.monthly_budget

    def _read_billing_period(self) -> BillingPeriodReading:
        """Read the injected billing boundary, tolerating one that raises."""
        try:
            return self._billing.billing_period_usage()
        except ProviderError:
            return BillingPeriodReading(None, MetricQuality.UNAVAILABLE)


def _fleet_unevaluable(snapshot: FleetTelemetrySnapshot) -> Iterator[Alert]:
    """Raise one fleet-scoped unevaluable alert per code a partial read blinded.

    Not defence in depth. ``FleetReconciler._missing`` suppresses itself
    *wholesale* on an enumeration that did not complete, so for
    ``missing_resource`` there is no per-subject signal at all and an absence
    of findings reads as a confident zero — precisely the fleet an operator
    most needs told about. The other three inventory-derived codes degrade
    per-subject but can still be silent for a subject nobody enumerated.

    ``stuck_deletion`` and ``budget_departure`` are outside this partition:
    their evidence is the durable store and the injected billing boundary, and
    a provider read that failed says nothing about either.
    """
    if snapshot.inventory_complete:
        return
    for code in sorted(_INVENTORY_DERIVED):
        yield Alert(
            code,
            None,
            Meter(None, MetricQuality.UNAVAILABLE),
            snapshot.observed_at,
        )
