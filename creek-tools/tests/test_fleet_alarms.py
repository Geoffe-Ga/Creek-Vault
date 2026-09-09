"""Report-only fleet alarms for issue #1769 (PR3).

ADR-0013 Decision 4 requires the control plane to raise an alarm on departure
from an operator-set monthly budget, and Decision 6 puts reconciliation and
alerts on the same footing. PR1 shipped the report-only reconciler, PR2 the
cost telemetry; neither compares anything to a threshold and
``tests/test_fleet_telemetry.py`` actively asserts the snapshot cannot spell an
alarm.

This suite pins five things at once. First, that an alarm has **subjects**, so
it is evaluated off the reconciliation report the single telemetry pass already
built rather than off a second provider read. Second, the rule that inverts
PR2's dominant defect class: an alarm whose threshold input was ``UNAVAILABLE``
emits an explicit unevaluable alert, never silence and never a compliant
reading — the *silent non-alert*. Third, that a raised alert with nothing behind
it is unconstructable. Fourth, that dedupe is per-run and stateless, so
``_SCHEMA_VERSION`` stays 4 and two passes over unchanged state compare equal.
Fifth, that the whole pass stays report-only, content-free and off
``/control/v1``.

The billing period is injected rather than derived: ``telemetry.py`` rules out
deriving :class:`BillingPeriodUsage` from an instant in terms, so PR3 adds a
third read-only Protocol whose shipped implementation answers unavailable
honestly.
"""

from __future__ import annotations

import ast
import dataclasses
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from creek_mcp import provisioning
from creek_mcp.provisioning.alerts import (
    _DIVERGENCE_ALERTS,
    Alert,
    AlertCode,
    AlertSink,
    BillingPeriodReading,
    BillingPeriodSource,
    FakeAlertSink,
    FleetAlarmError,
    FleetAlarms,
    UnavailableBillingPeriodSource,
    _code_for,
)
from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.inventory import (
    InventorySnapshot,
    MetricQuality,
    ProviderResource,
    ProviderResourceClass,
)
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.reconcile import DivergenceKind
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.telemetry import (
    BillingPeriodUsage,
    FleetPriceTable,
    Meter,
    estimate_monthly_cost,
)
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_api_support import (
    CONSUMER_TOKEN,
    PROVIDER_TOKEN,
    TLS_KEY,
    FakeFlyAPI,
    build_driver,
)
from tests.provisioning_report_only_support import (
    DYNAMIC_DISPATCH as _DYNAMIC_DISPATCH,
)
from tests.provisioning_report_only_support import (
    MUTATING_OPERATIONS as _MUTATING_OPERATIONS,
)
from tests.provisioning_report_only_support import durable_fingerprint
from tests.provisioning_secret_support import (
    FORBIDDEN_FIELD_NAMES,
    assert_content_free,
)
from tests.test_fleet_reconciliation import (
    _LIVE_ACTIVATION,
    _NOW,
    _ORPHAN_ACTIVATION,
    _POLICY,
    _plant_orphan,
    _protocol_methods,
    _provisioned,
    _surrogate,
)
from tests.test_fleet_telemetry import (
    _REFERENCE_PRICES,
    _machine_of,
    _telemetry,
)

if TYPE_CHECKING:
    from creek_mcp.provisioning.inventory import ProviderInventory

_PACKAGE: Final[Path] = (
    Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning"
)
_ALERTS_SOURCE: Final[str] = (_PACKAGE / "alerts.py").read_text(encoding="utf-8")

_CONSUMER: Final[str] = "adepthood-user-001"
_SECOND_ACTIVATION: Final[str] = "activation-C"
_REQUESTER: Final[str] = "adepthood"

# Both usages are INPUTS, exactly as _REFERENCE_PRICES is: they stand in for a
# billing-period export nobody can derive from an instant.
_UNDER_BUDGET_USAGE: Final[BillingPeriodUsage] = BillingPeriodUsage(
    volume_gb=Decimal(5),
    rootfs_gb=Decimal(1),
    running_hours=Decimal(0),
    egress_gb=Decimal(0),
    snapshot_gb=Decimal(0),
)
"""The ADR's fully-stopped allocation: $0.90 against a $10.00 budget."""

_OVER_BUDGET_USAGE: Final[BillingPeriodUsage] = BillingPeriodUsage(
    volume_gb=Decimal(100),
    rootfs_gb=Decimal(1),
    running_hours=Decimal(700),
    egress_gb=Decimal(0),
    snapshot_gb=Decimal(0),
)
"""A hundred GB of volume plus a nearly-continuous month: comfortably over."""

_EGRESS_UNMEASURED_USAGE: Final[BillingPeriodUsage] = BillingPeriodUsage(
    volume_gb=Decimal(60),
    rootfs_gb=Decimal(1),
    running_hours=Decimal(0),
    egress_gb=Decimal(0),
    snapshot_gb=Decimal(0),
)
"""An export whose egress meter lagged, zero-filled and flagged ESTIMATED.

``BillingPeriodUsage`` has five non-Optional ``Decimal`` fields, so a source
that could not read one of them **cannot express "unmeasured"**. Zero-filling
and degrading the reading's quality is the only honest signal the type leaves
it — which is exactly what makes this the realistic shape of a real billing
export, not a contrived one.
"""

_EGRESS_MEASURED_USAGE: Final[BillingPeriodUsage] = dataclasses.replace(
    _EGRESS_UNMEASURED_USAGE, egress_gb=Decimal(200)
)
"""The same period once the lagging egress meter caught up: over budget."""


class _UnattributedInventory:
    """Serve one billable resource whose attribution could not be read.

    ``ProviderResource.provider_allocation_id`` is ``str | None`` and
    ``ProviderInventory`` is an injected Protocol whose docstring anticipates
    "an offline invoice export rather than a live API". ``FlyProviderDriver``
    always attributes, but nothing in the type or the seam requires that of
    another implementation, and every divergence path skips ``None`` outright.
    """

    def list_resources(self) -> InventorySnapshot:
        """Return one Machine the pass can see but cannot attribute."""
        return InventorySnapshot(
            resources=(
                ProviderResource(
                    resource_class=ProviderResourceClass.MACHINE,
                    provider_id="machine-unattributed",
                    provider_allocation_id=None,
                    state="started",
                ),
            ),
            complete=True,
        )


class _StubBillingPeriodSource:
    """Supply a billing period from a source that is not the Machines API."""

    def __init__(self, reading: BillingPeriodReading) -> None:
        """Hold the figure an offline invoice export would have provided."""
        self._reading = reading

    def billing_period_usage(self) -> BillingPeriodReading:
        """Return the injected reading without a credential and without spend."""
        return self._reading


class _RaisingBillingPeriodSource:
    """Refuse the read the way a third-party implementation might."""

    def billing_period_usage(self) -> BillingPeriodReading:
        """Raise rather than reporting, exactly as ``_RaisingEgressMeter`` does."""
        raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)


class _RaisingAlertSink:
    """Refuse every delivery, so a raising sink's blast radius is provable."""

    def __init__(self) -> None:
        """Start the attempt counter at zero."""
        self.attempts = 0

    def deliver(self, alert: Alert) -> None:
        """Record the attempt, then refuse it."""
        del alert
        self.attempts += 1
        raise RuntimeError("sink refused delivery")


def _reading(usage: BillingPeriodUsage) -> BillingPeriodReading:
    """Wrap an injected usage record as an exactly-read billing period."""
    return BillingPeriodReading(usage, MetricQuality.EXACT)


def _alarms(
    store: ProvisioningStore,
    driver: ProviderInventory,
    *,
    billing: BillingPeriodSource | None = None,
    prices: FleetPriceTable | None = None,
    sink: AlertSink | None = None,
) -> FleetAlarms:
    """Bind one alarm pass over the operator store, the Fly fake and a budget."""
    return FleetAlarms(
        _telemetry(store, driver),
        prices=prices or _REFERENCE_PRICES,
        billing=billing or _StubBillingPeriodSource(_reading(_UNDER_BUDGET_USAGE)),
        sink=sink or FakeAlertSink(),
    )


def _raised(alarms: FleetAlarms, api: FakeFlyAPI | None = None) -> tuple[Alert, ...]:
    """Raise once, asserting the pass put nothing but GETs on the wire.

    Every test that alarms through the Fly fake goes through here rather than
    repeating the assertion, because the wire — not the AST tripwire — is what
    actually holds report-only.
    """
    baseline = 0 if api is None else len(api.requests)
    alerts = alarms.raise_alerts()
    if api is not None:
        assert {method for method, _ in api.requests[baseline:]} <= {"GET"}
    return alerts


def _notified(alarms: FleetAlarms, api: FakeFlyAPI) -> tuple[Alert, ...]:
    """Raise and deliver once, asserting the pass put nothing but GETs on the wire.

    The delivering counterpart of :func:`_raised`. Both exist so the
    report-only guarantee is carried by every provider-touching call site in
    this module rather than by the AST tripwire, which cannot enforce it.
    """
    baseline = len(api.requests)
    delivered = alarms.notify()
    assert {method for method, _ in api.requests[baseline:]} <= {"GET"}
    return delivered


def _codes(alerts: tuple[Alert, ...]) -> set[AlertCode]:
    """Return the distinct codes one pass raised."""
    return {alert.code for alert in alerts}


def test_a_running_machine_with_no_readable_clock_alarms_rather_than_falling_silent(
    tmp_path: Path,
) -> None:
    """The silent non-alert, made concrete and refused.

    ``FleetReconciler._is_running_beyond`` requires a readable
    ``last_modified_at``, so a Machine that has billed CPU for a month with no
    provider clock produces **no** ``RUNNING_BEYOND_POLICY`` divergence: it is
    surfaced only through ``FleetReconciliationReport.unmetered_running``. Any
    evaluator built off divergences alone therefore reports a clean fleet for
    the one allocation it could not measure.
    """
    store, api, _ = _provisioned(tmp_path)
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "started"
    surrogate = f"fly-{_surrogate(_LIVE_ACTIVATION)}"

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert raised == (
        Alert(
            AlertCode.CONTINUOUS_RUNNING,
            surrogate,
            Meter(None, MetricQuality.UNAVAILABLE),
            _NOW,
        ),
    )
    assert all(
        alert.contributing.quality is MetricQuality.UNAVAILABLE for alert in raised
    )


def test_one_orphan_raises_one_alert_not_one_per_resource(tmp_path: Path) -> None:
    """Dedupe is per (code, subject), and it needs no durable state to be it.

    One adopted orphan yields three ``ORPHAN_PROVIDER_RESOURCE`` divergences —
    app, Machine and volume. Three alerts for one condition is an operator
    paging three times; a count of one is a report that disagrees with the
    meter. The alert carries the reconciler's own count instead.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    alarms = _alarms(store, build_driver(api))

    first = _raised(alarms, api)
    second = _raised(alarms, api)

    assert first == (
        Alert(
            AlertCode.ORPHAN_RESOURCE,
            orphan,
            Meter(3, MetricQuality.EXACT),
            _NOW,
        ),
    )
    assert second == first


def test_an_unavailable_billing_period_never_reads_as_within_budget(
    tmp_path: Path,
) -> None:
    """The shipped billing source is honest, and honesty is an alert not a silence.

    ``UnavailableBillingPeriodSource`` is the correct answer for the Fly
    Machines API, which does not expose a billing period at all. Treating that
    as "no departure observed" would tell an operator their fleet is inside
    budget on evidence nobody has.
    """
    store, api, _ = _provisioned(tmp_path)

    raised = _raised(
        _alarms(store, build_driver(api), billing=UnavailableBillingPeriodSource()),
        api,
    )

    assert raised == (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(None, MetricQuality.UNAVAILABLE),
            _NOW,
        ),
    )
    assert raised[0].subject is None
    assert UnavailableBillingPeriodSource().billing_period_usage() == (
        BillingPeriodReading(None, MetricQuality.UNAVAILABLE)
    )


def test_the_alarm_surface_is_a_closed_set_covering_every_divergence_kind() -> None:
    """A sixth ``DivergenceKind`` must fail loudly, never escape the alarm surface.

    ``MISSING_PROVIDER_RESOURCE`` — a live allocation whose app the provider no
    longer lists — is a customer's vault gone while the store still bills for
    it, so it gains a code rather than being ruled unalarmed. Totality is
    asserted, which is strictly better than an exception list: an exception
    list is exactly what lets a seventh kind through.
    """
    assert set(AlertCode) == {
        AlertCode.DUPLICATE_ALLOCATION,
        AlertCode.ORPHAN_RESOURCE,
        AlertCode.MISSING_RESOURCE,
        AlertCode.STUCK_DELETION,
        AlertCode.CONTINUOUS_RUNNING,
        AlertCode.BUDGET_DEPARTURE,
    }
    assert set(_DIVERGENCE_ALERTS) == set(DivergenceKind)
    assert _code_for(DivergenceKind.MISSING_PROVIDER_RESOURCE) is (
        AlertCode.MISSING_RESOURCE
    )
    with pytest.raises(FleetAlarmError, match="unalarmed"):
        _code_for(cast("DivergenceKind", "gpu"))


def test_a_raised_alert_with_nothing_behind_it_is_unconstructable() -> None:
    """``Meter``'s biconditional is not enough, and the gap it leaves is the alarm.

    ``Meter(0, EXACT)`` satisfies "a value if and only if not unavailable"
    perfectly, and ``_measured([], complete=True)`` returns exactly it. An
    ``Alert`` carrying it is a raised alarm with zero contributors behind it.
    """
    with pytest.raises(ValueError, match="contributor"):
        Alert(
            AlertCode.DUPLICATE_ALLOCATION,
            "fly-000000000000000000000000",
            Meter(0, MetricQuality.EXACT),
            _NOW,
        )
    assert Alert(
        AlertCode.ORPHAN_RESOURCE,
        "fly-000000000000000000000000",
        Meter(1, MetricQuality.EXACT),
        _NOW,
    ).contributing == Meter(1, MetricQuality.EXACT)
    assert (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(None, MetricQuality.UNAVAILABLE),
            _NOW,
        ).subject
        is None
    )


def test_a_live_machine_in_an_unclassifiable_state_alarms_as_unevaluable(
    tmp_path: Path,
) -> None:
    """Neither known-running nor known-stopped is not the same claim as compliant."""
    store, api, _ = _provisioned(tmp_path)
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "stopping"
    surrogate = f"fly-{_surrogate(_LIVE_ACTIVATION)}"

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert raised == (
        Alert(
            AlertCode.CONTINUOUS_RUNNING,
            surrogate,
            Meter(None, MetricQuality.UNAVAILABLE),
            _NOW,
        ),
    )


def test_a_running_orphan_beyond_policy_is_reported_as_an_orphan_and_not_as_unevaluable(
    tmp_path: Path,
) -> None:
    """The named non-alert: the alarm never disagrees with the reconciler on scope.

    ``FleetReconciler._running`` is live-fenced, so ``RUNNING_BEYOND_POLICY``
    is deliberately out of scope for an orphan — PR1 judgement call 3. Feeding
    ``running_machine_seconds_by_allocation`` into the unevaluable set would
    invert the severity ordering: a running orphan whose clock is *unreadable*
    would alarm while one whose clock **is** readable and is months past policy
    would not, because the latter is in no unevaluable set at all.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    machine = api.machines[f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"][0]
    machine["updated_at"] = (_NOW - timedelta(days=30)).isoformat()

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert _codes(raised) == {AlertCode.ORPHAN_RESOURCE}
    assert raised == (
        Alert(AlertCode.ORPHAN_RESOURCE, orphan, Meter(3, MetricQuality.EXACT), _NOW),
    )


def test_an_unclassifiable_orphan_machine_is_not_claimed_unevaluable(
    tmp_path: Path,
) -> None:
    """``unclassified_machines`` is fleet-wide, so it is live-fenced by subtraction.

    ``FleetTelemetrySnapshot.unclassified_machines`` counts orphans too. Using
    it verbatim would raise a ``continuous_running`` unevaluable alert for a
    subject the reconciler ruled out of scope. Subtracting the orphan subjects
    is exact, because a surrogate absent from the live set yields an orphan
    divergence for every one of its resources.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    api.machines[f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"][0]["state"] = (
        "replacing"
    )

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert _codes(raised) == {AlertCode.ORPHAN_RESOURCE}
    assert raised[0].subject == orphan


def test_an_incomplete_enumeration_alarms_fleet_wide_rather_than_reading_clean(
    tmp_path: Path,
) -> None:
    """A rate-limited org listing is not evidence of a clean fleet.

    ``FleetReconciler._missing`` suppresses itself *wholesale* on a partial
    read, so ``missing_resource`` has no per-subject signal at all and an
    absence of findings would be a confident zero in operator terms. The
    store-sourced ``stuck_deletion`` is outside that partition and stays
    absent rather than being reported as an unevaluable it is not.
    """
    store, api, _ = _provisioned(tmp_path)
    api.fail_once("GET", "/v1/apps")

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert _codes(raised) == {
        AlertCode.DUPLICATE_ALLOCATION,
        AlertCode.ORPHAN_RESOURCE,
        AlertCode.MISSING_RESOURCE,
        AlertCode.CONTINUOUS_RUNNING,
    }
    assert all(alert.subject is None for alert in raised)
    assert all(
        alert.contributing == Meter(None, MetricQuality.UNAVAILABLE) for alert in raised
    )
    assert AlertCode.STUCK_DELETION not in _codes(raised)
    assert AlertCode.BUDGET_DEPARTURE not in _codes(raised)


def test_a_partial_read_reports_what_it_saw_beside_what_it_could_not_see(
    tmp_path: Path,
) -> None:
    """The fleet-scoped unevaluable alert does not replace the per-subject one.

    One app that rate-limits costs that app, not the whole observation, so the
    orphan that *was* enumerated still alarms — carrying ``ESTIMATED``, because
    a partial read makes its count a lower bound. The fleet-scoped unevaluable
    alert sits beside it saying the coverage of that code is unknown. Reporting
    only one of the two would either hide a finding or claim the finding is all
    there is.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    api.fail_once("GET", f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}/machines")

    raised = _raised(_alarms(store, build_driver(api)), api)

    per_subject = [alert for alert in raised if alert.subject is not None]
    fleet = [alert for alert in raised if alert.subject is None]
    assert per_subject == [
        Alert(
            AlertCode.ORPHAN_RESOURCE,
            orphan,
            Meter(3, MetricQuality.ESTIMATED),
            _NOW,
        )
    ]
    assert {alert.code for alert in fleet} == {
        AlertCode.DUPLICATE_ALLOCATION,
        AlertCode.ORPHAN_RESOURCE,
        AlertCode.MISSING_RESOURCE,
        AlertCode.CONTINUOUS_RUNNING,
    }
    assert all(
        alert.contributing == Meter(None, MetricQuality.UNAVAILABLE) for alert in fleet
    )


def test_two_duplicate_resources_under_one_allocation_raise_one_alert(
    tmp_path: Path,
) -> None:
    """The alert count comes from the reconciler, so the two cannot disagree."""
    store, api, _ = _provisioned(tmp_path)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.volumes[app_name].append(
        {
            "id": "vol-2",
            "name": f"fly-{_surrogate(_LIVE_ACTIVATION)}-vault",
            "region": "iad",
            "size_gb": 5,
            "encrypted": True,
            "state": "created",
        }
    )

    raised = _raised(_alarms(store, build_driver(api)), api)

    assert raised == (
        Alert(
            AlertCode.DUPLICATE_ALLOCATION,
            f"fly-{_surrogate(_LIVE_ACTIVATION)}",
            Meter(2, MetricQuality.EXACT),
            _NOW,
        ),
    )


def test_a_stuck_deletion_alarms_off_the_reconciler_not_re_derived(
    tmp_path: Path,
) -> None:
    """``stuck_deletion`` is store-sourced and survives a partial provider read."""
    store, api, job_id = _provisioned(tmp_path)
    store.request_delete(job_id, _REQUESTER, now=_NOW)
    later = _NOW + timedelta(hours=1)
    alarms = FleetAlarms(
        provisioning.FleetTelemetry(
            store,
            build_driver(api),
            _POLICY,
            egress=provisioning.UnavailableEgressMeter(),
            clock=lambda: later,
        ),
        prices=_REFERENCE_PRICES,
        billing=_StubBillingPeriodSource(_reading(_UNDER_BUDGET_USAGE)),
        sink=FakeAlertSink(),
    )

    raised = _raised(alarms, api)

    assert raised == (
        Alert(
            AlertCode.STUCK_DELETION,
            f"fly-{_surrogate(_LIVE_ACTIVATION)}",
            Meter(1, MetricQuality.EXACT),
            later,
        ),
    )
    assert raised[0].observed_at == later


def test_a_budget_departure_is_evaluated_only_from_an_injected_billing_period(
    tmp_path: Path,
) -> None:
    """A departure is fleet-scoped, and its subject is a typed sentinel."""
    store, api, _ = _provisioned(tmp_path)

    raised = _raised(
        _alarms(
            store,
            build_driver(api),
            billing=_StubBillingPeriodSource(_reading(_OVER_BUDGET_USAGE)),
        ),
        api,
    )

    assert raised == (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(1, MetricQuality.EXACT),
            _NOW,
        ),
    )


def test_the_budget_boundary_is_strict_and_a_compliant_period_stays_silent(
    tmp_path: Path,
) -> None:
    """A period exactly at the budget has not departed from it.

    Without both sides of the boundary the comparison can be inverted or
    loosened to ``>=`` and every other case stays green.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    at_budget = dataclasses.replace(_REFERENCE_PRICES, monthly_budget=Decimal("0.90"))
    under_budget = dataclasses.replace(
        _REFERENCE_PRICES, monthly_budget=Decimal("0.89")
    )
    billing = _StubBillingPeriodSource(_reading(_UNDER_BUDGET_USAGE))

    exactly = _raised(_alarms(store, driver, billing=billing, prices=at_budget), api)
    just_over = _raised(
        _alarms(store, driver, billing=billing, prices=under_budget), api
    )

    assert exactly == ()
    assert just_over == (
        Alert(AlertCode.BUDGET_DEPARTURE, None, Meter(1, MetricQuality.EXACT), _NOW),
    )


def test_a_lower_bound_under_budget_is_unevaluable_never_compliant(
    tmp_path: Path,
) -> None:
    """An approximate period under budget must not read as a measured clean month.

    This is PR2's own defect class arriving through the type system. The
    reading is ESTIMATED — a lower bound, exactly as ``_measured`` arm 3 and
    ``_lower_bound`` already use the word — so a lower bound *under* the budget
    rules nothing out. Discarding it as compliant makes a fleet that is really
    over budget byte-identical to a fully-measured month inside it.

    The lower bound over budget is a different case and stays a departure: an
    actual figure at or above a lower bound that already exceeds the budget
    exceeds it too.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    budget = _REFERENCE_PRICES.monthly_budget
    lower_bound = estimate_monthly_cost(_EGRESS_UNMEASURED_USAGE, _REFERENCE_PRICES)
    truth = estimate_monthly_cost(_EGRESS_MEASURED_USAGE, _REFERENCE_PRICES)

    raised = _raised(
        _alarms(
            store,
            driver,
            billing=_StubBillingPeriodSource(
                BillingPeriodReading(_EGRESS_UNMEASURED_USAGE, MetricQuality.ESTIMATED)
            ),
        ),
        api,
    )

    # The fixture is the real failure, not a contrivance: the figure the source
    # could report is under budget while the period it describes is over it.
    assert lower_bound < budget < truth
    assert raised == (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(1, MetricQuality.ESTIMATED),
            _NOW,
        ),
    )
    # Distinguishable from having no billing source at all, which is the
    # mirror defect: an approximate export must not look like no export.
    assert raised[0].contributing != Meter(None, MetricQuality.UNAVAILABLE)


def test_an_estimated_period_over_budget_is_still_a_departure(
    tmp_path: Path,
) -> None:
    """A lower bound already past the budget confirms the departure it reports."""
    store, api, _ = _provisioned(tmp_path)

    raised = _raised(
        _alarms(
            store,
            build_driver(api),
            billing=_StubBillingPeriodSource(
                BillingPeriodReading(_OVER_BUDGET_USAGE, MetricQuality.ESTIMATED)
            ),
        ),
        api,
    )

    assert raised == (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(1, MetricQuality.ESTIMATED),
            _NOW,
        ),
    )


def test_a_billing_source_that_raises_degrades_to_an_unevaluable_budget(
    tmp_path: Path,
) -> None:
    """An injected Protocol may raise; nothing is inferred from the refusal."""
    store, api, _ = _provisioned(tmp_path)

    raised = _raised(
        _alarms(store, build_driver(api), billing=_RaisingBillingPeriodSource()),
        api,
    )

    assert raised == (
        Alert(
            AlertCode.BUDGET_DEPARTURE,
            None,
            Meter(None, MetricQuality.UNAVAILABLE),
            _NOW,
        ),
    )


def test_a_reading_that_could_not_be_taken_is_unconstructable_as_a_usage() -> None:
    """``BillingPeriodReading`` carries ``Meter``'s biconditional, not a weaker one."""
    with pytest.raises(ValueError, match="unavailable"):
        BillingPeriodReading(_UNDER_BUDGET_USAGE, MetricQuality.UNAVAILABLE)
    with pytest.raises(ValueError, match="unavailable"):
        BillingPeriodReading(None, MetricQuality.EXACT)
    assert BillingPeriodReading(None, MetricQuality.UNAVAILABLE).usage is None
    assert (
        BillingPeriodReading(_UNDER_BUDGET_USAGE, MetricQuality.ESTIMATED).quality
        is MetricQuality.ESTIMATED
    )


def test_alerts_are_totally_ordered_and_content_free(tmp_path: Path) -> None:
    """One pass raising three different conditions is stable and says nothing."""
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    live = f"fly-{_surrogate(_LIVE_ACTIVATION)}"
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "stopping"
    api.volumes[f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"].append(
        {
            "id": "vol-2",
            "name": f"{live}-vault",
            "region": "iad",
            "size_gb": 5,
            "encrypted": True,
            "state": "created",
        }
    )
    sink = FakeAlertSink()
    alarms = _alarms(store, build_driver(api), sink=sink)

    raised = _raised(alarms, api)
    delivered = _notified(alarms, api)

    assert [(alert.code, alert.subject) for alert in raised] == [
        (AlertCode.CONTINUOUS_RUNNING, live),
        (AlertCode.DUPLICATE_ALLOCATION, live),
        (AlertCode.ORPHAN_RESOURCE, orphan),
    ]
    assert delivered == raised
    assert sink.delivered == raised
    rendered = (
        repr(raised)
        + str([dataclasses.asdict(alert) for alert in raised])
        + repr(sink.delivered)
    )
    assert_content_free(rendered)
    for secret in (_LIVE_ACTIVATION, _ORPHAN_ACTIVATION, TLS_KEY, CONSUMER_TOKEN):
        assert secret not in rendered
    assert PROVIDER_TOKEN not in rendered


def test_two_passes_at_different_instants_still_raise_an_equal_tuple(
    tmp_path: Path,
) -> None:
    """``observed_at`` is excluded from equality, and that is worth proving.

    Drawing both passes from one frozen clock would make the purity claim true
    for the wrong reason: the timestamps would be equal anyway, so removing
    ``field(compare=False)`` would change nothing. The instant is nonetheless
    *carried*, because cross-run suppression is the sink's contract and any
    time-boxed policy needs one.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    driver = build_driver(api)
    later = _NOW + timedelta(days=3)
    billing = _StubBillingPeriodSource(_reading(_UNDER_BUDGET_USAGE))

    first = _raised(_alarms(store, driver, billing=billing), api)
    second = _raised(
        FleetAlarms(
            provisioning.FleetTelemetry(
                store,
                driver,
                _POLICY,
                egress=provisioning.UnavailableEgressMeter(),
                clock=lambda: later,
            ),
            prices=_REFERENCE_PRICES,
            billing=billing,
            sink=FakeAlertSink(),
        ),
        api,
    )

    assert first[0].observed_at != second[0].observed_at
    assert second[0].observed_at == later
    assert first == second
    assert {alert.subject for alert in first} == {orphan}


def test_the_sink_declares_one_method_and_a_repeat_delivery_is_idempotent(
    tmp_path: Path,
) -> None:
    """Cross-run suppression is the sink's contract, never durable alarm state."""
    store, api, _ = _provisioned(tmp_path)
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    sink = FakeAlertSink()
    alarms = _alarms(store, build_driver(api), sink=sink)

    first = _notified(alarms, api)
    second = _notified(alarms, api)

    assert _protocol_methods(AlertSink) == {"deliver"}
    assert _protocol_methods(BillingPeriodSource) == {"billing_period_usage"}
    assert first == second
    assert len(sink.delivered) == 1
    assert sink.suppressed_count == 1


def test_a_sink_that_raises_neither_truncates_nor_corrupts_the_raised_tuple(
    tmp_path: Path,
) -> None:
    """The tuple is computed before any delivery, and no exception is swallowed."""
    store, api, _ = _provisioned(tmp_path)
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    sink = _RaisingAlertSink()
    alarms = _alarms(store, build_driver(api), sink=sink)
    expected = _raised(alarms, api)
    baseline = len(api.requests)

    with pytest.raises(RuntimeError, match="refused delivery"):
        alarms.notify()

    # This one site cannot use _notified, because it never returns; the same
    # wire assertion is made inline so no provider-touching call in this
    # module is exempt from it.
    assert {method for method, _ in api.requests[baseline:]} <= {"GET"}
    assert sink.attempts == 1
    assert _raised(alarms, api) == expected
    assert len(expected) == 1


def test_the_alarm_pass_writes_nothing_durable_and_leaves_the_schema_at_four(
    tmp_path: Path,
) -> None:
    """PR3 takes no schema version, so 5 stays reserved for PR4's receipts.

    Cross-run dedupe would need durable state *and* would break the
    equal-passes purity property PR1 and PR2 both hold: a suppression ledger
    makes the second pass emit nothing.

    The fingerprint is the load-bearing assertion, and the schema pin alone was
    not. ``PRAGMA user_version`` proves no *migration* ran; an ``INSERT`` leaves
    it untouched, names no method any allowlist enumerates and puts nothing on
    the provider wire, so a raw write escaped the tripwire, the wire assertion
    and the schema pin at once. Hashing the file asserts the outcome instead of
    the syntax, so it holds whatever spelling reached the database.
    """
    database = tmp_path / "provisioning.sqlite3"
    store, api, _ = _provisioned(tmp_path)
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    before = durable_fingerprint(database)

    _raised(_alarms(store, build_driver(api)), api)

    assert durable_fingerprint(database) == before
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
    assert version[0] == 4
    for forbidden in ("CREATE TABLE", "ALTER", "PRAGMA", "user_version", "_SCHEMA"):
        assert forbidden not in _ALERTS_SOURCE


def test_the_durable_fingerprint_would_notice_a_raw_row_write(
    tmp_path: Path,
) -> None:
    """The guard above is only worth having if it can fail.

    A schema-version pin cannot tell these two states apart; the fingerprint
    can, which is the whole reason the previous test does not rest on the pin.
    """
    database = tmp_path / "provisioning.sqlite3"
    store, _, _ = _provisioned(tmp_path)
    before = durable_fingerprint(database)

    store.submit(_SECOND_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
    assert version[0] == 4
    assert durable_fingerprint(database) != before


def test_a_resource_that_could_not_be_attributed_is_never_silently_dropped(
    tmp_path: Path,
) -> None:
    """A billable resource with no readable attribution reaches no alarm at all.

    ``FleetReconciler._orphans``, ``._duplicates`` and ``._unmetered`` all skip
    ``provider_allocation_id is None``, and so does
    ``_unclassified_machines``. So the resource is enumerated, billed for, and
    invisible to every code — absence of signal reading as absence of problem,
    one notch inside the ``UNAVAILABLE`` boundary rather than across it.

    It blinds the same four inventory-derived codes a partial enumeration
    does, so it raises the same four fleet-scoped unevaluable alerts, and the
    count itself is surfaced on the snapshot rather than left to inference.
    """
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    inventory = _UnattributedInventory()
    alarms = _alarms(store, inventory)

    snapshot = _telemetry(store, inventory).observe()
    raised = _raised(alarms)

    assert snapshot.inventory_complete is True
    assert snapshot.unattributed_resources == Meter(1, MetricQuality.EXACT)
    assert _codes(raised) == {
        AlertCode.DUPLICATE_ALLOCATION,
        AlertCode.ORPHAN_RESOURCE,
        AlertCode.MISSING_RESOURCE,
        AlertCode.CONTINUOUS_RUNNING,
    }
    assert all(alert.subject is None for alert in raised)
    assert all(
        alert.contributing == Meter(None, MetricQuality.UNAVAILABLE) for alert in raised
    )


def test_a_fully_attributed_complete_pass_raises_no_coverage_alarm(
    tmp_path: Path,
) -> None:
    """The blinding trigger is load-bearing, not a constant True."""
    store, api, _ = _provisioned(tmp_path)

    snapshot = _telemetry(store, build_driver(api)).observe()
    raised = _raised(_alarms(store, build_driver(api)), api)

    assert snapshot.unattributed_resources == Meter(0, MetricQuality.EXACT)
    assert raised == ()


def test_the_fake_driver_proves_report_only_without_a_provider(
    tmp_path: Path,
) -> None:
    """Every criterion is provable without a credential, a network or spend."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    store.submit(_LIVE_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    driver.adopt_orphan("fake-orphaned-allocation")

    raised = _raised(_alarms(store, driver))

    assert driver.delete_count == 0
    assert raised == (
        Alert(
            AlertCode.ORPHAN_RESOURCE,
            "fake-orphaned-allocation",
            Meter(3, MetricQuality.EXACT),
            _NOW,
        ),
    )


def test_reported_alarm_models_carry_no_credential_or_activation_field() -> None:
    """Content-freedom is structural: no field can hold a secret or a preimage.

    Deliberate rather than incidental: ``OperatorAllocationView`` *does* carry
    ``consumer_identity``, so an alarm that projected an allocation view rather
    than a surrogate would publish it.
    """
    declared = {
        field.name
        for model in (Alert, BillingPeriodReading)
        for field in dataclasses.fields(model)
    }

    assert declared.isdisjoint(FORBIDDEN_FIELD_NAMES)
    assert not any("activation" in name for name in declared)
    assert "consumer_identity" not in declared


def test_no_price_or_conversion_base_is_hard_coded_in_the_alerts_module() -> None:
    """ADR-0013 Decision 4: reference prices are injected, never business logic."""
    figures = (
        "0.75",
        "0.15",
        "0.0082",
        "0.00822",
        "0.90",
        "0.94",
        "1.14",
        "1.86",
        "6.67",
        "720",
        "10**9",
    )
    for figure in figures:
        assert figure not in _ALERTS_SOURCE


def test_the_billing_period_is_never_derived_from_a_telemetry_snapshot() -> None:
    """A lower-bound instant fed into a monthly figure would fabricate it.

    ``telemetry.py`` rules that out in terms, so the module may name
    ``BillingPeriodUsage`` as a type but must never construct one, and the
    budget path must not be able to reach a snapshot at all.
    """
    tree = ast.parse(_ALERTS_SOURCE)
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    budget = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_budget_alerts"
    )

    assert "BillingPeriodUsage" not in constructed
    assert "FleetTelemetrySnapshot" not in ast.unparse(budget)
    assert "estimate_monthly_cost" in constructed
    assert "monthly_budget" in _ALERTS_SOURCE


_ALERTS_IMPORTS: Final[frozenset[str]] = frozenset(
    {
        "annotations",
        "defaultdict",
        "dataclass",
        "field",
        "StrEnum",
        "unique",
        "Lock",
        "TYPE_CHECKING",
        "Final",
        "Protocol",
        "ProviderError",
        "MetricQuality",
        "DivergenceKind",
        "Meter",
        "_measured",
        "estimate_monthly_cost",
        "Iterator",
        "Mapping",
        "datetime",
        "Decimal",
        "BillingPeriodUsage",
        "FleetPriceTable",
        "FleetTelemetry",
        "FleetTelemetrySnapshot",
    }
)
"""Every name alerts.py may import. All of them are read-only or inert.

Notably absent: ``ProvisioningStore``, ``ProviderDriver`` and
``OperatorAllocationView``. The last is what makes surrogate-only attribution
structural rather than conventional — that model carries ``consumer_identity``.
"""


def test_the_alerts_module_trips_on_the_spellings_of_a_repair_path() -> None:
    """A tripwire over three syntactic shapes. Not a proof, and materially leaky.

    Four of these have now been written across three PRs and every one shipped
    a docstring that outran its walk, so this one states the walk itself rather
    than the property someone hoped it implied.

    It parses ``alerts.py`` and checks exactly three things about that one
    file: the set of imported names equals ``_ALERTS_IMPORTS``; the ``attr`` of
    every ``ast.Attribute`` and the ``id`` of every **called** ``ast.Name`` are
    disjoint from the shared mutating-operation set; and the same two
    collections are disjoint from the dynamic-dispatch set plus
    ``__dict__``/``__class__``. That is the entire content.

    Three holes are named because they are reachable, not to be exhaustive:

    * **Dynamic dispatch escapes by rebinding.** Called names are collected
      only in call position, so ``dispatch = getattr`` then ``dispatch(...)``
      passes: ``getattr`` never appears as a called name and never as an
      attribute.
    * **A raw store write escapes entirely.** ``sqlite3``-style ``connect`` and
      ``execute`` are in neither set, an ``INSERT`` moves no schema version and
      touches no provider, so the store-seam names in the allowlist give no
      coverage against SQL that never spells one of them.
    * **It is one file.** Nothing here says anything about any other module.

    Because of that the real guarantees are behavioural and are the assertions
    to trust:

    * *The wire.* Every call in this module that reaches a **provider fake**
      goes through ``_raised`` or ``_notified``, which assert the pass put
      nothing but GETs on it; the one site that cannot — the sink whose
      ``deliver`` raises, so nothing returns — asserts it inline after the
      exception. The single call passing no ``api`` is the
      ``FakeProviderDriver`` test, which has no wire to observe and asserts
      ``delete_count == 0`` instead. That is the exhaustive list.
    * *The store.* ``test_the_alarm_pass_writes_nothing_durable...`` fingerprints
      the whole database file across a pass, which catches a durable write
      whatever spelling reached it, and a companion test proves that
      fingerprint can actually fail.
    * *The capability.* ``AlertSink`` declares exactly one method, so a sink is
      never handed a mutation capability in the first place.
    """
    tree = ast.parse(_ALERTS_SOURCE)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called_attributes = {node.attr for node in calls if isinstance(node, ast.Attribute)}
    called_names = {node.id for node in calls if isinstance(node, ast.Name)}
    referenced = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert imported == _ALERTS_IMPORTS
    assert called_attributes.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_MUTATING_OPERATIONS)
    assert referenced.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_DYNAMIC_DISPATCH)
    assert referenced.isdisjoint(
        _DYNAMIC_DISPATCH | {"__getattr__", "__getattribute__", "__dict__", "__class__"}
    )


def test_alarms_ship_as_a_library_and_add_nothing_to_the_consumer_api() -> None:
    """Fleet alarms are cross-consumer operator data; /control/v1 holds one bearer.

    The existing telemetry check greps those three files for the literal
    ``telemetry`` only, so it would not have caught an ``alerts`` import — the
    substring is per-module and nothing extends it automatically.
    """
    exported = {
        "Alert",
        "AlertCode",
        "AlertSink",
        "BillingPeriodReading",
        "BillingPeriodSource",
        "FakeAlertSink",
        "FleetAlarmError",
        "FleetAlarms",
        "UnavailableBillingPeriodSource",
    }
    httpapi = (
        Path(__file__).resolve().parents[1] / "creek_mcp" / "httpapi"
    ) / "provisioning.py"

    assert exported <= set(provisioning.__all__)
    assert list(provisioning.__all__) == sorted(provisioning.__all__)
    for module in (_PACKAGE / "cli.py", _PACKAGE / "api.py", httpapi):
        text = module.read_text(encoding="utf-8")
        assert "alerts" not in text
        assert "alarm" not in text
