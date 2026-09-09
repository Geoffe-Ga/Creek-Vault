"""Fleet cost telemetry and budget configuration for issue #1769 (PR2).

ADR-0013 Decision 4 requires the control plane to *report* provisioned volumes,
stopped rootfs capacity, active Machine seconds, snapshot bytes and egress so an
operator can reconcile an actual invoice, and it names "operations documentation
and billing tests" as the only two homes for its reference prices. PR1 shipped
the enumeration and the report-only reconciler; nothing yet turns either into a
cost observation, and ``MetricQuality.ESTIMATED`` had no producer at all.

This suite pins four things at once: one pure observation that writes nothing
and mutates nothing, a degradation rule under which a meter nobody could read is
never indistinguishable from one read and found compliant, a budget/price
configuration that is injected rather than hard-coded, and the content-freedom
of everything the observation emits. The Machine ``config`` mapping this PR
newly reads carries the plaintext activation id *and* base64 TLS key material,
so several assertions below exist only to catch an implementation that lets any
of it through.

Alarms are deliberately absent: PR3 owns them, and a test below asserts the
snapshot cannot even spell one.
"""

from __future__ import annotations

import ast
import dataclasses
import sqlite3
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from creek_mcp import provisioning
from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.inventory import (
    InventorySnapshot,
    MetricQuality,
    ProviderInventory,
)
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.telemetry import (
    AllocationMeter,
    BillingPeriodUsage,
    EgressMeter,
    FleetPriceTable,
    FleetTelemetry,
    FleetTelemetrySnapshot,
    Meter,
    UnavailableEgressMeter,
    estimate_monthly_cost,
    storage_bytes,
)
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_api_support import (
    CONSUMER_TOKEN,
    PROVIDER_TOKEN,
    TLS_KEY,
    FakeFlyAPI,
    build_driver,
)
from tests.provisioning_secret_support import (
    FORBIDDEN_FIELD_NAMES,
    assert_content_free,
)
from tests.test_fleet_reconciliation import (
    _LIVE_ACTIVATION,
    _NOW,
    _ORPHAN_ACTIVATION,
    _POLICY,
    _MalformedAppListing,
    _plant_orphan,
    _protocol_methods,
    _provisioned,
    _surrogate,
)

if TYPE_CHECKING:
    from creek_mcp.provisioning.fly import FlyProviderDriver

_PACKAGE: Final[Path] = (
    Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning"
)
_TELEMETRY_SOURCE: Final[str] = (_PACKAGE / "telemetry.py").read_text(encoding="utf-8")

_SECOND_ACTIVATION: Final[str] = "activation-C"
_CONSUMER: Final[str] = "adepthood-user-001"
_REQUESTER: Final[str] = "adepthood"

# ADR-0013 Decision 4's published Fly.io figures, supplied here as INPUTS.
# They are assumptions about a third party's price list, so this file and
# docs/provisioning-control-plane.md are the only two places they may live.
_REFERENCE_PRICES: Final[FleetPriceTable] = FleetPriceTable(
    monthly_budget=Decimal("10.00"),
    volume_gb_month=Decimal("0.15"),
    stopped_rootfs_gb_month=Decimal("0.15"),
    running_machine_hour=Decimal("0.0082"),
    egress_gb=Decimal("0.02"),
    snapshot_gb_month=Decimal("0.15"),
    hours_per_month=Decimal(720),
    bytes_per_gb=10**9,
)


def _telemetry(
    store: ProvisioningStore,
    driver: ProviderInventory,
    *,
    egress: EgressMeter | None = None,
) -> FleetTelemetry:
    """Bind one pure observation over the operator store and the Fly fake."""
    return FleetTelemetry(
        store,
        driver,
        _POLICY,
        egress=egress or UnavailableEgressMeter(),
        clock=lambda: _NOW,
    )


class _StubEgressMeter:
    """Supply an egress figure from a source that is not the Machines API."""

    def __init__(self, meter: Meter) -> None:
        """Hold the figure an offline invoice export would have provided."""
        self._meter = meter

    def egress_bytes(self) -> Meter:
        """Return the injected figure without a credential and without spend."""
        return self._meter


class _RaisingEgressMeter:
    """Refuse the read the way a third-party implementation might."""

    def egress_bytes(self) -> Meter:
        """Raise rather than reporting, exactly as ``_FailingInventory`` does."""
        raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)


def _machine_of(api: FakeFlyAPI, activation_id: str) -> dict[str, object]:
    """Return the single Machine dict the fake holds for *activation_id*."""
    machines = api.machines[f"creek-vault-{_surrogate(activation_id)}"]
    assert len(machines) == 1
    return machines[0]


def test_every_required_meter_is_reported_with_its_quality(tmp_path: Path) -> None:
    """All seven items are measured, and no unreadable meter reads as zero."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    baseline = len(api.requests)

    snapshot = _telemetry(store, driver).observe()

    assert snapshot.activated_allocations == Meter(1, MetricQuality.EXACT)
    assert snapshot.provisioned_volumes == Meter(1, MetricQuality.EXACT)
    assert snapshot.stopped_rootfs_gb == Meter(1, MetricQuality.EXACT)
    assert snapshot.volume_gb == Meter(5, MetricQuality.EXACT)
    assert snapshot.snapshot_bytes == Meter(0, MetricQuality.EXACT)
    assert snapshot.egress_bytes == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.running_machine_seconds == Meter(0, MetricQuality.ESTIMATED)
    assert snapshot.running_machine_seconds_by_allocation == ()
    assert snapshot.duplicate_allocation_attempts == Meter(0, MetricQuality.EXACT)
    assert snapshot.refused_allocation_attempts == Meter(
        None, MetricQuality.UNAVAILABLE
    )
    assert snapshot.orphan_provider_resources == Meter(0, MetricQuality.EXACT)
    assert snapshot.unconfirmed_deletions == Meter(0, MetricQuality.EXACT)
    assert snapshot.unmetered_running == ()
    assert snapshot.inventory_complete is True
    assert snapshot.observed_at == _NOW
    assert _telemetry(store, driver).observe() == snapshot
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}
    rendered = repr(snapshot) + str(dataclasses.asdict(snapshot))
    assert_content_free(rendered)
    for secret in (_LIVE_ACTIVATION, TLS_KEY, CONSUMER_TOKEN, PROVIDER_TOKEN):
        assert secret not in rendered


def test_a_meter_that_could_not_be_read_is_unconstructable_as_a_zero() -> None:
    """``Meter`` refuses both halves of "unavailable" disagreeing with a value."""
    with pytest.raises(ValueError, match="unavailable"):
        Meter(0, MetricQuality.UNAVAILABLE)
    with pytest.raises(ValueError, match="unavailable"):
        Meter(None, MetricQuality.EXACT)
    with pytest.raises(ValueError, match="unavailable"):
        Meter(None, MetricQuality.ESTIMATED)
    assert Meter(0, MetricQuality.EXACT).value == 0
    assert Meter(None, MetricQuality.UNAVAILABLE).value is None


def test_a_rootfs_no_machine_reports_degrades_instead_of_summing_to_zero(
    tmp_path: Path,
) -> None:
    """A meter nothing could read is UNAVAILABLE, never a compliant-looking 0.

    ``config.rootfs`` is a key Creek writes in ``_machine_request`` and reads
    back; it is not a documented Fly response field, so "every contributor
    unreadable while the enumeration completed" is the expected production
    case, not an edge case. Summing it to 0 and labelling it ESTIMATED would
    report a fleet of billing rootfs volumes as costing nothing.
    """
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    machine = _machine_of(api, _LIVE_ACTIVATION)
    config = machine["config"]
    assert isinstance(config, dict)
    del config["rootfs"]

    snapshot = _telemetry(store, driver).observe()

    assert snapshot.stopped_rootfs_gb == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.volume_gb == Meter(5, MetricQuality.EXACT)
    assert snapshot.inventory_complete is True


def test_a_partly_readable_capacity_is_an_estimated_lower_bound(
    tmp_path: Path,
) -> None:
    """Some-but-not-all readable is a lower bound, never a silent total."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    orphan_app = f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    api.machines[orphan_app][0]["state"] = "stopped"

    snapshot = _telemetry(store, driver).observe()

    assert snapshot.stopped_rootfs_gb == Meter(1, MetricQuality.ESTIMATED)
    assert snapshot.provisioned_volumes == Meter(2, MetricQuality.EXACT)
    assert snapshot.orphan_provider_resources == Meter(3, MetricQuality.EXACT)


def test_running_machine_seconds_are_an_estimated_lower_bound(
    tmp_path: Path,
) -> None:
    """ESTIMATED gains its first producer, per allocation and fleet-wide."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    machine = _machine_of(api, _LIVE_ACTIVATION)
    machine["state"] = "started"
    machine["updated_at"] = (_NOW - timedelta(hours=2)).isoformat()
    baseline = len(api.requests)

    snapshot = _telemetry(store, driver).observe()

    surrogate = f"fly-{_surrogate(_LIVE_ACTIVATION)}"
    assert snapshot.running_machine_seconds == Meter(7200, MetricQuality.ESTIMATED)
    assert snapshot.running_machine_seconds_by_allocation == (
        AllocationMeter(subject=surrogate, meter=Meter(7200, MetricQuality.ESTIMATED)),
    )
    assert snapshot.stopped_rootfs_gb == Meter(0, MetricQuality.EXACT)
    assert snapshot.unmetered_running == ()
    assert {method for method, _ in api.requests[baseline:]} == {"GET"}


def test_running_machines_with_no_readable_clock_are_never_zero_seconds(
    tmp_path: Path,
) -> None:
    """Every running Machine unreadable is UNAVAILABLE, and stays visible."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "started"

    snapshot = _telemetry(store, driver).observe()

    assert snapshot.running_machine_seconds == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.running_machine_seconds_by_allocation == (
        AllocationMeter(
            subject=f"fly-{_surrogate(_LIVE_ACTIVATION)}",
            meter=Meter(None, MetricQuality.UNAVAILABLE),
        ),
    )
    assert snapshot.unmetered_running == (f"fly-{_surrogate(_LIVE_ACTIVATION)}",)


def test_egress_is_a_third_protocol_that_needs_no_credential(
    tmp_path: Path,
) -> None:
    """Egress lives on Fly's billing surface, so the seam is injected, not widened."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)

    assert _protocol_methods(ProviderDriver) == {"provision", "delete"}
    assert _protocol_methods(ProviderInventory) == {"list_resources"}
    assert _protocol_methods(EgressMeter) == {"egress_bytes"}
    assert UnavailableEgressMeter().egress_bytes() == Meter(
        None, MetricQuality.UNAVAILABLE
    )
    raised = _telemetry(store, driver, egress=_RaisingEgressMeter()).observe()
    assert raised.egress_bytes == Meter(None, MetricQuality.UNAVAILABLE)
    supplied = _telemetry(
        store,
        driver,
        egress=_StubEgressMeter(Meter(4_096, MetricQuality.EXACT)),
    ).observe()
    assert supplied.egress_bytes == Meter(4_096, MetricQuality.EXACT)


def test_a_rate_limited_enumeration_never_reads_as_a_small_clean_fleet(
    tmp_path: Path,
) -> None:
    """A partial or malformed read degrades every provider-sourced figure."""
    store, api, _ = _provisioned(tmp_path)
    api.fail_once("GET", "/v1/apps")
    failed = _telemetry(store, build_driver(api)).observe()
    malformed = _telemetry(store, build_driver(_MalformedAppListing.of(api))).observe()

    for snapshot in (failed, malformed):
        assert snapshot.inventory_complete is False
        assert snapshot.provisioned_volumes == Meter(None, MetricQuality.UNAVAILABLE)
        assert snapshot.stopped_rootfs_gb == Meter(None, MetricQuality.UNAVAILABLE)
        assert snapshot.volume_gb == Meter(None, MetricQuality.UNAVAILABLE)
        assert snapshot.snapshot_bytes == Meter(None, MetricQuality.UNAVAILABLE)
        assert snapshot.orphan_provider_resources == Meter(
            None, MetricQuality.UNAVAILABLE
        )
        assert snapshot.running_machine_seconds == Meter(
            None, MetricQuality.UNAVAILABLE
        )
        # Store-sourced figures do not degrade: the durable side was readable.
        assert snapshot.activated_allocations == Meter(1, MetricQuality.EXACT)
        assert snapshot.unconfirmed_deletions == Meter(0, MetricQuality.EXACT)
        assert snapshot.duplicate_allocation_attempts == Meter(0, MetricQuality.EXACT)


def test_a_partial_enumeration_still_reports_what_it_did_observe(
    tmp_path: Path,
) -> None:
    """One app that rate-limits costs that app, not the whole observation."""
    store, api, _ = _provisioned(tmp_path)
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    orphan_app = f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"
    api.machines[orphan_app][0]["state"] = "stopped"
    api.fail_once("GET", f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}/machines")

    snapshot = _telemetry(store, build_driver(api)).observe()

    assert snapshot.inventory_complete is False
    assert snapshot.stopped_rootfs_gb == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.volume_gb == Meter(5, MetricQuality.ESTIMATED)
    assert snapshot.orphan_provider_resources == Meter(3, MetricQuality.ESTIMATED)


def test_stopped_rootfs_capacity_needs_no_call_the_reconciler_does_not_make(
    tmp_path: Path,
) -> None:
    """The rootfs figure comes from the Machines listing PR1 already fetched."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    baseline = len(api.requests)

    snapshot = _telemetry(store, driver).observe()

    assert {path for _, path in api.requests[baseline:]} == {
        "/v1/apps",
        f"/v1/apps/{app_name}/machines",
        f"/v1/apps/{app_name}/volumes",
        f"/v1/apps/{app_name}/volumes/vol-1/snapshots",
    }
    assert snapshot.stopped_rootfs_gb == Meter(1, MetricQuality.EXACT)
    resources = driver.list_resources().resources
    rendered = repr(resources) + str(
        [dataclasses.asdict(resource) for resource in resources]
    )
    assert_content_free(rendered)
    for secret in (_LIVE_ACTIVATION, TLS_KEY, CONSUMER_TOKEN):
        assert secret not in rendered


def test_duplicate_allocation_attempts_are_counted_without_a_schema_change(
    tmp_path: Path,
) -> None:
    """A second alias under one live consumer is durable and read-only countable.

    A pure replay contributes nothing — that is the idempotency contract
    working, not a duplicate allocation — and an attempt the store *refused*
    leaves no durable trace at all, which is why the refused meter is
    permanently unavailable rather than a zero.
    """
    database = tmp_path / "provisioning.sqlite3"
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)

    assert _telemetry(store, driver).observe().duplicate_allocation_attempts == Meter(
        0, MetricQuality.EXACT
    )
    store.submit(_LIVE_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    assert _telemetry(store, driver).observe().duplicate_allocation_attempts == Meter(
        0, MetricQuality.EXACT
    )
    store.submit(_SECOND_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    snapshot = _telemetry(store, driver).observe()

    assert snapshot.duplicate_allocation_attempts == Meter(1, MetricQuality.EXACT)
    assert snapshot.refused_allocation_attempts == Meter(
        None, MetricQuality.UNAVAILABLE
    )
    assert snapshot.refused_allocation_attempts.value is None
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
    assert version[0] == 4


def test_the_price_table_supplies_no_figure_of_its_own() -> None:
    """Budget thresholds are configuration: no field has a default to fall back on."""
    required = {
        field.name
        for field in dataclasses.fields(FleetPriceTable)
        if field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    }

    assert required == {
        "monthly_budget",
        "volume_gb_month",
        "stopped_rootfs_gb_month",
        "running_machine_hour",
        "egress_gb",
        "snapshot_gb_month",
        "hours_per_month",
        "bytes_per_gb",
    }
    with pytest.raises(ValueError, match="must not be negative"):
        dataclasses.replace(_REFERENCE_PRICES, egress_gb=Decimal("-0.01"))
    with pytest.raises(ValueError, match="must be positive"):
        dataclasses.replace(_REFERENCE_PRICES, hours_per_month=Decimal(0))
    with pytest.raises(ValueError, match="must be positive"):
        dataclasses.replace(_REFERENCE_PRICES, bytes_per_gb=0)
    with pytest.raises(ValueError, match="must be positive"):
        dataclasses.replace(_REFERENCE_PRICES, monthly_budget=Decimal(0))
    with pytest.raises(ValueError, match="must not be negative"):
        BillingPeriodUsage(
            volume_gb=Decimal(5),
            rootfs_gb=Decimal(1),
            running_hours=Decimal(-1),
            egress_gb=Decimal(0),
            snapshot_gb=Decimal(0),
        )


def test_no_price_is_hard_coded_in_the_telemetry_module() -> None:
    """ADR-0013 Decision 4: reference prices are injected, never business logic."""
    for figure in ("0.75", "0.15", "0.0082", "0.90", "0.94", "1.14", "1.86", "6.67"):
        assert figure not in _TELEMETRY_SOURCE


def _reference_cost(running_hours: Decimal) -> Decimal:
    """Estimate one allocation's monthly cost from the ADR's own inputs."""
    usage = BillingPeriodUsage(
        volume_gb=Decimal(5),
        rootfs_gb=Decimal(1),
        running_hours=running_hours,
        egress_gb=Decimal(0),
        snapshot_gb=Decimal(0),
    )
    return estimate_monthly_cost(usage, _REFERENCE_PRICES).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def test_the_estimator_reproduces_the_adr_reference_figures() -> None:
    """ADR-0013 Decision 4's table, reproduced from injected inputs at 720 hours.

    Continuously started is pinned at $6.65, not the ADR's published $6.67:
    at the 720-hour month that reproduces the other four figures exactly the
    arithmetic gives 6.6540, and 730 hours gives 6.7360, so $6.67 implies
    721.95 hours. ``hours_per_month`` is deliberately NOT tuned to make the
    published figure pass — doing so would smuggle the hard-coded business
    assumption Decision 4 forbids into the one number nobody checks.
    """
    assert _reference_cost(Decimal(0)) == Decimal("0.90")
    assert _reference_cost(Decimal(30) * Decimal(10) / Decimal(60)) == Decimal("0.94")
    assert _reference_cost(Decimal(30)) == Decimal("1.14")
    assert _reference_cost(Decimal(120)) == Decimal("1.86")
    assert _reference_cost(Decimal(720)) == Decimal("6.65")
    assert _reference_cost(Decimal(1000)) == Decimal("8.95")


def test_storage_bytes_rolls_up_only_through_the_injected_conversion_base(
    tmp_path: Path,
) -> None:
    """GB and bytes are separate invoice lines; the base is never inline."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.snapshots[(app_name, "vol-1")].append({"id": "snap-1", "size": 4096})

    snapshot = _telemetry(store, driver).observe()

    assert snapshot.volume_gb == Meter(5, MetricQuality.EXACT)
    assert snapshot.snapshot_bytes == Meter(4096, MetricQuality.EXACT)
    assert storage_bytes(snapshot, _REFERENCE_PRICES) == Meter(
        5 * 10**9 + 4096, MetricQuality.EXACT
    )
    binary = dataclasses.replace(_REFERENCE_PRICES, bytes_per_gb=2**30)
    assert storage_bytes(snapshot, binary) == Meter(
        5 * 2**30 + 4096, MetricQuality.EXACT
    )


def test_storage_bytes_degrades_with_whichever_meter_degraded(
    tmp_path: Path,
) -> None:
    """A rollup is never cleaner than the weakest figure that fed it."""
    store, api, _ = _provisioned(tmp_path)
    api.fail_once("GET", "/v1/apps")
    unavailable = _telemetry(store, build_driver(api)).observe()
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    api.volumes[f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"][0].pop("size_gb")
    partial = _telemetry(store, build_driver(api)).observe()

    assert storage_bytes(unavailable, _REFERENCE_PRICES) == Meter(
        None, MetricQuality.UNAVAILABLE
    )
    assert storage_bytes(partial, _REFERENCE_PRICES) == Meter(
        5 * 10**9, MetricQuality.ESTIMATED
    )


def test_reported_models_carry_no_credential_or_activation_field() -> None:
    """Content-freedom is structural: no field can hold a secret or a preimage."""
    declared = {
        field.name
        for model in (
            FleetTelemetrySnapshot,
            Meter,
            AllocationMeter,
            FleetPriceTable,
            BillingPeriodUsage,
        )
        for field in dataclasses.fields(model)
    }

    assert declared.isdisjoint(FORBIDDEN_FIELD_NAMES)
    assert not any("activation" in name for name in declared)
    assert "consumer_identity" not in declared


def test_the_snapshot_cannot_spell_an_alarm_and_never_reads_the_budget() -> None:
    """PR3 owns alarms. PR2 measures, and nothing here consults a threshold."""
    declared = {field.name for field in dataclasses.fields(FleetTelemetrySnapshot)}
    tree = ast.parse(_TELEMETRY_SOURCE)
    price_table = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "FleetPriceTable"
    )
    inside_table = set(map(id, ast.walk(price_table)))
    budget_references = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
        and (getattr(node, "attr", None) or getattr(node, "id", None))
        == "monthly_budget"
    ]

    assert declared.isdisjoint(
        {
            "verdict",
            "severity",
            "alert",
            "alarm",
            "breach",
            "threshold",
            "over_budget",
            "budget",
        }
    )
    assert budget_references
    assert all(id(node) in inside_table for node in budget_references)


_TELEMETRY_IMPORTS: Final[frozenset[str]] = frozenset(
    {
        "annotations",
        "defaultdict",
        "dataclass",
        "field",
        "Decimal",
        "TYPE_CHECKING",
        "Protocol",
        "ProviderError",
        "InventorySnapshot",
        "MetricQuality",
        "ProviderResourceClass",
        "DivergenceKind",
        "FleetReconciler",
        "_RUNNING_STATES",
        "Callable",
        "Sequence",
        "datetime",
        "ProviderInventory",
        "ProviderResource",
        "FleetReconcilePolicy",
        "FleetReconciliationReport",
        "ProvisioningStore",
    }
)
"""Every name telemetry.py may import. All of them are read-only or inert."""

_MUTATING_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"provision", "delete", "start", "stop", "delete_orphan"}
)

_DYNAMIC_DISPATCH: Final[frozenset[str]] = frozenset(
    {"getattr", "setattr", "vars", "eval", "exec", "__import__", "globals"}
)


def test_the_telemetry_module_trips_on_the_spellings_of_a_repair_path() -> None:
    """A tripwire over the common spellings, not a proof, and not presented as one.

    It has its OWN allowlist: ``_RECONCILE_IMPORTS`` is pinned by exact
    equality in the reconciliation suite, so widening it to cover this module
    would silently license a new import over there too. Dependency runs one
    way — telemetry imports reconcile, never the reverse — and that direction
    is asserted below rather than assumed.

    The behavioural guarantee lives on the wire instead: every telemetry test
    asserts the pass issued nothing but GETs, and the fake driver's teardown
    counter stays at zero.
    """
    tree = ast.parse(_TELEMETRY_SOURCE)
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
    reconcile_source = (_PACKAGE / "reconcile.py").read_text(encoding="utf-8")

    assert imported == _TELEMETRY_IMPORTS
    assert called_attributes.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_MUTATING_OPERATIONS)
    assert referenced.isdisjoint(_MUTATING_OPERATIONS)
    assert called_names.isdisjoint(_DYNAMIC_DISPATCH)
    assert referenced.isdisjoint(
        _DYNAMIC_DISPATCH | {"__getattr__", "__getattribute__"}
    )
    assert "telemetry" not in reconcile_source


def test_the_fake_driver_proves_report_only_without_a_provider(
    tmp_path: Path,
) -> None:
    """Every criterion is provable without a credential, a network or spend."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    driver = FakeProviderDriver()
    store.submit(_LIVE_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    driver.adopt_orphan("fake-orphaned-allocation")

    snapshot = _telemetry(store, driver).observe()

    assert driver.delete_count == 0
    assert snapshot.activated_allocations == Meter(1, MetricQuality.EXACT)
    assert snapshot.orphan_provider_resources.value == 3
    # FakeProviderDriver reports no sizes at all, so every contributor is
    # unreadable — the case a summing implementation would call a clean zero.
    assert snapshot.stopped_rootfs_gb == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.volume_gb == Meter(None, MetricQuality.UNAVAILABLE)


def test_telemetry_ships_as_a_library_and_adds_nothing_to_the_consumer_api() -> None:
    """Fleet aggregates are operator data; /control/v1 holds one bearer only."""
    exported = {
        "AllocationMeter",
        "BillingPeriodUsage",
        "EgressMeter",
        "FleetPriceTable",
        "FleetTelemetry",
        "FleetTelemetrySnapshot",
        "Meter",
        "UnavailableEgressMeter",
        "estimate_monthly_cost",
        "storage_bytes",
    }
    httpapi = (
        Path(__file__).resolve().parents[1] / "creek_mcp" / "httpapi"
    ) / "provisioning.py"

    assert exported <= set(provisioning.__all__)
    assert list(provisioning.__all__) == sorted(provisioning.__all__)
    for module in (_PACKAGE / "cli.py", _PACKAGE / "api.py", httpapi):
        assert "telemetry" not in module.read_text(encoding="utf-8")


def test_an_inventory_boundary_that_raises_is_an_unavailable_fleet(
    tmp_path: Path,
) -> None:
    """A third-party Protocol implementation may raise; nothing is inferred."""
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")

    class _FailingInventory:
        def list_resources(self) -> InventorySnapshot:
            raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)

    snapshot = _telemetry(store, _FailingInventory()).observe()

    assert snapshot.inventory_complete is False
    assert snapshot.activated_allocations == Meter(0, MetricQuality.EXACT)
    assert snapshot.volume_gb == Meter(None, MetricQuality.UNAVAILABLE)


def test_a_stuck_deletion_is_counted_off_the_reconciler_not_re_derived(
    tmp_path: Path,
) -> None:
    """Items 6 and 7 reuse the reconciler so PR3 and PR4 cannot disagree with it."""
    store, api, job_id = _provisioned(tmp_path)
    driver: FlyProviderDriver = build_driver(api)
    store.request_delete(job_id, _REQUESTER, now=_NOW)
    later = _NOW + timedelta(hours=1)

    snapshot = FleetTelemetry(
        store, driver, _POLICY, egress=UnavailableEgressMeter(), clock=lambda: later
    ).observe()

    assert snapshot.unconfirmed_deletions == Meter(1, MetricQuality.EXACT)
    assert snapshot.orphan_provider_resources == Meter(0, MetricQuality.EXACT)
    assert snapshot.observed_at == later
