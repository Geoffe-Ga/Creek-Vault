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
from creek_mcp.provisioning.models import FailureReason, OperatorAllocationView
from creek_mcp.provisioning.reconcile import DivergenceKind, FleetReconciler
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
    _ReplayStore,
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
    running_machine_hour=Decimal("0.00822"),
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


def _observe(
    telemetry: FleetTelemetry,
    api: FakeFlyAPI | None = None,
) -> FleetTelemetrySnapshot:
    """Observe once, asserting the pass put nothing but GETs on the wire.

    Every test that observes through the Fly fake goes through here rather
    than repeating the assertion, because the wire — not the AST tripwire — is
    what actually holds report-only, and a guarantee asserted in 2 of 23 tests
    is not the guarantee the tripwire's docstring used to claim.
    """
    baseline = 0 if api is None else len(api.requests)
    snapshot = telemetry.observe()
    if api is not None:
        assert {method for method, _ in api.requests[baseline:]} <= {"GET"}
    return snapshot


def _machine_of(api: FakeFlyAPI, activation_id: str) -> dict[str, object]:
    """Return the single Machine dict the fake holds for *activation_id*."""
    machines = api.machines[f"creek-vault-{_surrogate(activation_id)}"]
    assert len(machines) == 1
    return machines[0]


def test_every_required_meter_is_reported_with_its_quality(tmp_path: Path) -> None:
    """All seven items are measured, and no unreadable meter reads as zero."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)

    snapshot = _observe(_telemetry(store, driver), api)

    assert snapshot.activated_allocations == Meter(1, MetricQuality.EXACT)
    assert snapshot.provisioned_volumes == Meter(1, MetricQuality.EXACT)
    assert snapshot.stopped_rootfs_gb == Meter(1, MetricQuality.ESTIMATED)
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
    assert _observe(_telemetry(store, driver), api) == snapshot
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

    snapshot = _observe(_telemetry(store, driver), api)

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

    snapshot = _observe(_telemetry(store, driver), api)

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

    snapshot = _observe(_telemetry(store, driver), api)

    surrogate = f"fly-{_surrogate(_LIVE_ACTIVATION)}"
    assert snapshot.running_machine_seconds == Meter(7200, MetricQuality.ESTIMATED)
    assert snapshot.running_machine_seconds_by_allocation == (
        AllocationMeter(subject=surrogate, meter=Meter(7200, MetricQuality.ESTIMATED)),
    )
    assert snapshot.stopped_rootfs_gb == Meter(0, MetricQuality.ESTIMATED)
    assert snapshot.unmetered_running == ()


def test_running_machines_with_no_readable_clock_are_never_zero_seconds(
    tmp_path: Path,
) -> None:
    """Every running Machine unreadable is UNAVAILABLE, and stays visible."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "started"

    snapshot = _observe(_telemetry(store, driver), api)

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
    raised = _observe(_telemetry(store, driver, egress=_RaisingEgressMeter()), api)
    assert raised.egress_bytes == Meter(None, MetricQuality.UNAVAILABLE)
    supplied = _observe(
        _telemetry(
            store,
            driver,
            egress=_StubEgressMeter(Meter(4_096, MetricQuality.EXACT)),
        ),
        api,
    )
    assert supplied.egress_bytes == Meter(4_096, MetricQuality.EXACT)


def test_a_rate_limited_enumeration_never_reads_as_a_small_clean_fleet(
    tmp_path: Path,
) -> None:
    """A partial or malformed read degrades every provider-sourced figure."""
    store, api, _ = _provisioned(tmp_path)
    api.fail_once("GET", "/v1/apps")
    failed = _observe(_telemetry(store, build_driver(api)), api)
    malformed = _observe(
        _telemetry(store, build_driver(_MalformedAppListing.of(api))), api
    )

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

    snapshot = _observe(_telemetry(store, build_driver(api)), api)

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

    snapshot = _observe(_telemetry(store, driver), api)

    assert {path for _, path in api.requests[baseline:]} == {
        "/v1/apps",
        f"/v1/apps/{app_name}/machines",
        f"/v1/apps/{app_name}/volumes",
        f"/v1/apps/{app_name}/volumes/vol-1/snapshots",
    }
    assert snapshot.stopped_rootfs_gb == Meter(1, MetricQuality.ESTIMATED)
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

    first = _observe(_telemetry(store, driver), api)
    assert first.duplicate_allocation_attempts == Meter(0, MetricQuality.EXACT)
    store.submit(_LIVE_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    replayed = _observe(_telemetry(store, driver), api)
    assert replayed.duplicate_allocation_attempts == Meter(0, MetricQuality.EXACT)
    store.submit(_SECOND_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)
    snapshot = _observe(_telemetry(store, driver), api)

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
    )
    for figure in figures:
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
    """ADR-0013 Decision 4's whole table, reproduced from injected inputs.

    All five figures reproduce, including the continuously-started $6.67. The
    hourly rate is supplied at the precision the ADR's own table implies —
    ``0.00822``, whose display value to four places is the published
    ``$0.0082`` — and ``720`` is supplied as an injected month. ``0.75 +
    720 * 0.00822 = 6.6684``, which quantizes to ``$6.67``.

    ``hours_per_month`` is an *assumption*, not a derivation: the first four
    figures reproduce at 720, 730 and 744 alike, so they cannot distinguish a
    month. Only the continuous case can, and it is the reason 720 is the value
    supplied here — 730 gives $6.75 and 744 gives $6.87.
    """
    assert _reference_cost(Decimal(0)) == Decimal("0.90")
    assert _reference_cost(Decimal(30) * Decimal(10) / Decimal(60)) == Decimal("0.94")
    assert _reference_cost(Decimal(30)) == Decimal("1.14")
    assert _reference_cost(Decimal(120)) == Decimal("1.86")
    assert _reference_cost(Decimal(720)) == Decimal("6.67")
    assert _reference_cost(Decimal(1000)) == Decimal("8.97")
    longer = dataclasses.replace(_REFERENCE_PRICES, hours_per_month=Decimal(730))
    continuous = estimate_monthly_cost(
        BillingPeriodUsage(
            volume_gb=Decimal(5),
            rootfs_gb=Decimal(1),
            running_hours=Decimal(730),
            egress_gb=Decimal(0),
            snapshot_gb=Decimal(0),
        ),
        longer,
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert continuous == Decimal("6.75")


def test_storage_bytes_rolls_up_only_through_the_injected_conversion_base(
    tmp_path: Path,
) -> None:
    """GB and bytes are separate invoice lines; the base is never inline."""
    store, api, _ = _provisioned(tmp_path)
    driver = build_driver(api)
    app_name = f"creek-vault-{_surrogate(_LIVE_ACTIVATION)}"
    api.snapshots[(app_name, "vol-1")].append({"id": "snap-1", "size": 4096})

    snapshot = _observe(_telemetry(store, driver), api)

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
    unavailable = _observe(_telemetry(store, build_driver(api)), api)
    _plant_orphan(api, _ORPHAN_ACTIVATION)
    api.volumes[f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"][0].pop("size_gb")
    partial = _observe(_telemetry(store, build_driver(api)), api)

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
        "Final",
        "OperatorAllocationView",
        "Sequence",
        "datetime",
        "timedelta",
        "ProviderInventory",
        "ProviderResource",
        "FleetReconcilePolicy",
        "FleetReconciliationReport",
        "ProvisioningStore",
    }
)
"""Every name telemetry.py may import. All of them are read-only or inert."""

_MUTATING_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        # The provider seam.
        "provision",
        "delete",
        "start",
        "stop",
        "delete_orphan",
        # The durable seam. A store mutation reaches provider deletion
        # through the worker, so covering only the driver leaves a whole
        # path a telemetry pass must never take.
        "submit",
        "request_delete",
        "retry",
        "record_failure",
        "complete_create",
        "complete_delete",
        "claim_next",
        "complete_key_ceremony",
        "expire_key_ceremonies",
    }
)

_DYNAMIC_DISPATCH: Final[frozenset[str]] = frozenset(
    {"getattr", "setattr", "vars", "eval", "exec", "__import__", "globals"}
)


def test_the_telemetry_module_trips_on_the_spellings_of_a_repair_path() -> None:
    """A tripwire over the common spellings, not a proof, and not presented as one.

    What it actually enforces: no attribute call, bare-name call or attribute
    reference in ``telemetry.py`` names a mutating operation on **either**
    seam — the provider driver's ``provision``/``delete``/``start``/``stop``
    or the durable store's ``submit``/``request_delete``/``claim_next`` and
    the rest — no dynamic-dispatch builtin is called, and the module's import
    set is exactly the listed read-only names. Covering only the driver would
    leave the store seam open, and a store mutation reaches provider deletion
    through the worker.

    What it cannot enforce: attribute access has spellings this does not
    enumerate, and no syntax-level check closes that set. PR1's equivalent
    tripwire was reviewed as overclaiming and so was the first draft of this
    one, which said "every telemetry test" asserts GET-only when two did.

    It has its OWN allowlist: ``_RECONCILE_IMPORTS`` is pinned by exact
    equality in the reconciliation suite, so widening it to cover this module
    would silently license a new import over there too. Dependency runs one
    way — telemetry imports reconcile, never the reverse — and that direction
    is asserted below rather than assumed.

    The behavioural guarantee lives on the wire instead, and is now true as
    stated: every test in this module that observes through the Fly fake goes
    through the ``_observe`` helper, which asserts the pass put nothing but
    GETs on the wire; ``_ReplayStore`` is proven to raise rather than reach
    SQLite; and the ``FakeProviderDriver`` test asserts a zero teardown count.
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
        _DYNAMIC_DISPATCH | {"__getattr__", "__getattribute__", "__dict__", "__class__"}
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

    snapshot = _observe(_telemetry(store, driver))

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

    snapshot = _observe(_telemetry(store, _FailingInventory()))

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

    snapshot = _observe(
        FleetTelemetry(
            store,
            driver,
            _POLICY,
            egress=UnavailableEgressMeter(),
            clock=lambda: later,
        ),
        api,
    )

    assert snapshot.unconfirmed_deletions == Meter(1, MetricQuality.EXACT)
    assert snapshot.orphan_provider_resources == Meter(0, MetricQuality.EXACT)
    assert snapshot.observed_at == later


class _RecordingStore(ProvisioningStore):
    """Count how many times one pass reads the durable live view."""

    def __init__(self, database: Path) -> None:
        """Open the real store and start the read counter at zero."""
        super().__init__(database)
        self.live_reads = 0

    def live_allocations(self) -> list[OperatorAllocationView]:
        """Record the read, then answer it from the real database."""
        self.live_reads += 1
        return super().live_allocations()


class _SettlingStore(_RecordingStore):
    """Answer the second live read as if a delete settled mid-pass.

    Not a synthetic proxy: ``ProvisioningStore._connect`` opens a fresh
    autocommit connection per call and ``ProvisioningWorker`` is a concurrent
    writer by design, so two reads inside one pass genuinely see two different
    committed states.
    """

    def live_allocations(self) -> list[OperatorAllocationView]:
        """Return the real view once, then an emptied fleet."""
        rows = super().live_allocations()
        return [] if self.live_reads > 1 else rows


def test_the_durable_live_view_is_read_exactly_once_per_pass(
    tmp_path: Path,
) -> None:
    """Two reads of a concurrently-written table make one snapshot self-contradictory.

    With a second read, a fleet that settles mid-pass is reported as one live
    allocation whose every provider resource is simultaneously an orphan — the
    same Machine attributed to a live allocation by the duration meter and to
    nobody by the divergence meter, in one snapshot an operator would act on.
    """
    _provisioned(tmp_path)
    database = tmp_path / "provisioning.sqlite3"
    api = FakeFlyAPI()
    settling = _SettlingStore(database)
    counting = _RecordingStore(database)

    _observe(_telemetry(counting, build_driver(api)), api)
    snapshot = _observe(_telemetry(settling, build_driver(api)), api)

    assert counting.live_reads == 1
    assert settling.live_reads == 1
    assert snapshot.activated_allocations == Meter(1, MetricQuality.EXACT)
    assert snapshot.orphan_provider_resources == Meter(0, MetricQuality.EXACT)


def test_the_replay_store_cannot_reach_a_durable_mutation(tmp_path: Path) -> None:
    """The adapter the pass hands the reconciler holds no database at all."""
    store, _, _ = _provisioned(tmp_path)
    replay = _ReplayStore(store.live_allocations(), [])

    assert replay.live_allocations() == store.live_allocations()
    assert replay.unconfirmed_deletions(timedelta(minutes=15), now=_NOW) == []
    with pytest.raises(AttributeError):
        replay.submit(_SECOND_ACTIVATION, _CONSUMER, _REQUESTER, now=_NOW)


def test_a_running_orphan_is_metered_rather_than_invisible(tmp_path: Path) -> None:
    """The single most expensive thing #1769 exists to surface must be visible.

    Live-fencing the duration meter while every capacity meter stays
    fleet-wide made an orphan burning CPU for a month byte-identical to a
    healthy idle fleet: ``Meter(0, ESTIMATED)`` with an empty per-allocation
    tuple. An orphan already carries a surrogate, so it is attributed by it.
    """
    store, api, _ = _provisioned(tmp_path)
    orphan = _plant_orphan(api, _ORPHAN_ACTIVATION)
    machine = api.machines[f"creek-vault-{_surrogate(_ORPHAN_ACTIVATION)}"][0]
    machine["state"] = "started"
    machine["updated_at"] = (_NOW - timedelta(days=30)).isoformat()

    snapshot = _observe(_telemetry(store, build_driver(api)), api)

    month = 30 * 24 * 60 * 60
    assert snapshot.running_machine_seconds == Meter(month, MetricQuality.ESTIMATED)
    assert snapshot.running_machine_seconds_by_allocation == (
        AllocationMeter(subject=orphan, meter=Meter(month, MetricQuality.ESTIMATED)),
    )
    assert snapshot.orphan_provider_resources == Meter(3, MetricQuality.EXACT)


def test_an_unclassifiable_machine_state_is_never_counted_as_stopped(
    tmp_path: Path,
) -> None:
    """ "Not running" is not the same claim as "stopped, and billing rootfs".

    A negated running set silently classes every state Creek does not know —
    ``replacing``, ``stopping``, a state Fly adds next year — as stopped, and
    reports its rootfs at a confident quality. The predicate is a closed
    positive set instead, and anything outside both sets contributes nothing
    and is named.
    """
    store, api, _ = _provisioned(tmp_path)
    _machine_of(api, _LIVE_ACTIVATION)["state"] = "replacing"

    snapshot = _observe(_telemetry(store, build_driver(api)), api)

    assert snapshot.stopped_rootfs_gb == Meter(None, MetricQuality.UNAVAILABLE)
    surrogate = f"fly-{_surrogate(_LIVE_ACTIVATION)}"
    assert snapshot.unclassified_machines == (surrogate,)
    # Nothing here knows whether a replacing Machine bills for CPU, so the
    # duration meter degrades too rather than counting it as zero seconds.
    assert snapshot.running_machine_seconds == Meter(None, MetricQuality.UNAVAILABLE)
    assert snapshot.running_machine_seconds_by_allocation == (
        AllocationMeter(
            subject=surrogate, meter=Meter(None, MetricQuality.UNAVAILABLE)
        ),
    )
    assert snapshot.volume_gb == Meter(5, MetricQuality.EXACT)


def test_duplicate_provider_resources_are_counted_off_the_reconciler(
    tmp_path: Path,
) -> None:
    """Two second volumes billing under one allocation is not a zero.

    ``duplicate_allocation_attempts`` counts folded activation aliases in the
    durable store — a different thing entirely — so a pass whose own report
    holds DUPLICATE_ALLOCATION divergences used to report "duplicate
    allocations: 0, exact" beside them.
    """
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

    snapshot = _observe(_telemetry(store, build_driver(api)), api)

    report = FleetReconciler(
        store, build_driver(api), _POLICY, clock=lambda: _NOW
    ).reconcile()
    duplicates = [
        divergence
        for divergence in report.divergences
        if divergence.kind is DivergenceKind.DUPLICATE_ALLOCATION
    ]

    assert len(duplicates) == 2
    assert snapshot.duplicate_provider_resources == Meter(2, MetricQuality.EXACT)
    assert snapshot.duplicate_allocation_attempts == Meter(0, MetricQuality.EXACT)
    assert snapshot.volume_gb == Meter(10, MetricQuality.EXACT)
