"""Operator-injected budget policy, cost estimate, alerts, and D7 review (#1769).

Every rate, budget, duration and threshold below is an injected test fixture.
ADR-0013 Decision 4 figures appear only in the one billing test that names
them, and the scanner test proves they never enter production code.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from creek_mcp.provisioning.budget import (
    Alert,
    AlertKind,
    CostEstimate,
    FleetPolicy,
    InjectedUsage,
    ReviewTrigger,
    RunningBasis,
    estimate_monthly_cost,
    evaluate_alerts,
    evaluate_review_checkpoint,
    load_policy_file,
)
from creek_mcp.provisioning.models import (
    Disposition,
    Divergence,
    DivergenceKind,
    FleetTelemetry,
    ResourceClass,
)

_PROVISIONING = Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning"
_GIB = 1024**3
_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_POLICY: dict[str, Any] = {
    "currency": "USD",
    "monthly_budget": "100.00",
    "volume_gb_month_rate": "0.20",
    "stopped_rootfs_gb_month_rate": "0.10",
    "running_hour_rate": "0.01",
    "snapshot_gb_month_rate": None,
    "egress_gb_rate": None,
    "max_continuous_running_seconds": 7200,
    "stuck_deletion_seconds": 1800,
    "review_activated_vaults": 10,
    "review_provisioned_volumes": 20,
    "review_months_over_budget": 2,
}
_REFERENCE_PRICE_RE = re.compile(
    r"(?<![\w.])(0\.75|0\.15|0\.0082|2\.02|6\.67|0\.90|1\.14|1\.86)(?![\w.])"
)


def _policy(**overrides: Any) -> FleetPolicy:
    """Return the injected reference policy with *overrides*."""
    return FleetPolicy.from_mapping({**_POLICY, **overrides})


def _telemetry(**overrides: Any) -> FleetTelemetry:
    """Return a small seeded fleet measurement with *overrides*."""
    values: dict[str, Any] = {
        "activated_allocations": 1,
        "allocations_by_state": {"ready": 1},
        "provisioned_volumes": 1,
        "volume_bytes": 5 * _GIB,
        "stopped_rootfs_gb": 1,
        "machines_without_rootfs_size": 0,
        "running_machine_seconds_by_allocation": {},
        "running_machine_seconds_fleet": 0,
        "running_seconds_injected": None,
        "snapshot_bytes": None,
        "egress_bytes": None,
        "duplicate_allocation_attempts": 0,
        "orphan_resources": 0,
        "unconfirmed_deletions": 0,
        "oldest_unconfirmed_deletion_seconds": None,
        "sources": {},
    }
    values.update(overrides)
    return FleetTelemetry(**values)


def _reference_price_literals(path: Path) -> list[str]:
    """Return every ADR-0013 D4 figure that appears as a literal in *path*."""
    return _REFERENCE_PRICE_RE.findall(path.read_text(encoding="utf-8"))


def test_fleet_policy_requires_every_operator_value_and_validates(
    tmp_path: Path,
) -> None:
    """No rate, budget, duration or threshold has a default; bad values fail."""
    no_arguments: tuple[Any, ...] = ()
    with pytest.raises(TypeError):
        FleetPolicy(*no_arguments)
    for key in _POLICY:
        if key in {"snapshot_gb_month_rate", "egress_gb_rate"}:
            continue
        missing = {name: value for name, value in _POLICY.items() if name != key}
        with pytest.raises(ValueError, match=key):
            FleetPolicy.from_mapping(missing)
    for bad in (
        {"monthly_budget": "0"},
        {"volume_gb_month_rate": "-0.01"},
        {"running_hour_rate": 0.01},
        {"stuck_deletion_seconds": 0},
        {"max_continuous_running_seconds": -1},
        {"currency": "  "},
        {"review_activated_vaults": 0},
        {"review_months_over_budget": 0},
        {"snapshot_gb_month_rate": "0"},
        {"monthly_budget": "NaN"},
        {"monthly_budget": "Infinity"},
        {"running_hour_rate": Decimal("-Infinity")},
        {"egress_gb_rate": "sNaN"},
    ):
        with pytest.raises((ValueError, TypeError), match=next(iter(bad))):
            _policy(**bad)
    policy = _policy()
    assert policy.max_continuous_running == timedelta(hours=2)
    assert policy.stuck_deletion_after == timedelta(minutes=30)
    assert policy.monthly_budget == Decimal("100.00")
    assert policy.snapshot_gb_month_rate is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.__setattr__("currency", "EUR")

    toml = tmp_path / "policy.toml"
    toml.write_text(
        "[budget]\ncurrency = 'USD'\nmonthly_budget = 100.00\n"
        "volume_gb_month_rate = 0.20\nstopped_rootfs_gb_month_rate = 0.10\n"
        "running_hour_rate = 0.1\nsnapshot_gb_month_rate = 0.02\n"
        "[policy]\nmax_continuous_running_seconds = 7200\n"
        "stuck_deletion_seconds = 1800\n"
        "[review]\nactivated_vaults = 10\nprovisioned_volumes = 20\n"
        "months_over_budget = 2\n"
        "[usage]\nsnapshot_bytes = 1073741824\nrunning_seconds = 3600\n",
        encoding="utf-8",
    )
    loaded = load_policy_file(toml)
    assert FleetPolicy.from_toml(toml) == loaded.policy
    assert loaded.policy.running_hour_rate == Decimal("0.1")
    assert str(loaded.policy.running_hour_rate) == "0.1"
    assert loaded.policy.snapshot_gb_month_rate == Decimal("0.02")
    assert loaded.usage == InjectedUsage(
        snapshot_bytes=_GIB, egress_bytes=None, running_seconds=3600
    )
    toml.write_text("[budget]\ncurrency = 'USD'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="monthly_budget"):
        load_policy_file(toml)
    toml.write_text("not toml at all = = =", encoding="utf-8")
    with pytest.raises(ValueError, match="TOML"):
        load_policy_file(toml)


def test_no_reference_price_literal_lives_in_provisioning_code(tmp_path: Path) -> None:
    """ADR-0013 D4 figures are injected assumptions, never business logic."""
    scanned = sorted(_PROVISIONING.rglob("*.py"))
    assert {path.name for path in scanned} >= {
        "budget.py",
        "reconcile.py",
        "fleet_cli.py",
        "fleet_schema.py",
        "fly.py",
        "store.py",
        "driver.py",
        "worker.py",
    }
    offenders = {
        path.name: literals
        for path in scanned
        if (literals := _reference_price_literals(path))
    }
    assert offenders == {}

    violating = tmp_path / "violating.py"
    violating.write_text("RUNNING_HOUR_RATE = 0.0082\n", encoding="utf-8")
    assert _reference_price_literals(violating) == ["0.0082"]
    innocent = tmp_path / "innocent.py"
    innocent.write_text("VERSION = '1.1.0'\nX = 10.15\nY = 0.150\n", encoding="utf-8")
    assert _reference_price_literals(innocent) == []


def test_estimate_uses_injected_then_projected_then_month_to_date_running_hours() -> (
    None
):
    """Compute is billed from the best available basis and says which one."""
    policy = _policy()
    sampled = _telemetry(running_machine_seconds_fleet=3600)
    early = datetime(2026, 9, 1, 12, tzinfo=UTC)
    one_day_in = datetime(2026, 9, 2, tzinfo=UTC)

    month_to_date = estimate_monthly_cost(sampled, policy, now=early)
    projected = estimate_monthly_cost(sampled, policy, now=one_day_in)
    injected = estimate_monthly_cost(
        _telemetry(running_machine_seconds_fleet=3600, running_seconds_injected=7200),
        policy,
        now=early,
    )

    assert isinstance(month_to_date, CostEstimate)
    assert month_to_date.running_basis is RunningBasis.MONTH_TO_DATE
    assert month_to_date.components["running"] == Decimal("0.01")
    assert projected.running_basis is RunningBasis.PROJECTED
    assert projected.components["running"] == Decimal("0.30")
    assert injected.running_basis is RunningBasis.INJECTED
    assert injected.components["running"] == Decimal("0.02")
    assert month_to_date.components["volume"] == Decimal("1.00")
    assert month_to_date.components["stopped_rootfs"] == Decimal("0.10")
    assert month_to_date.estimated_month == Decimal("1.11")
    assert month_to_date.unpriced == ("egress", "snapshot")
    assert "snapshot" not in month_to_date.components
    assert month_to_date.currency == "USD"

    priced = estimate_monthly_cost(
        _telemetry(snapshot_bytes=2 * _GIB, egress_bytes=10 * _GIB),
        _policy(snapshot_gb_month_rate="0.05", egress_gb_rate="0.02"),
        now=early,
    )
    assert priced.components["snapshot"] == Decimal("0.10")
    assert priced.components["egress"] == Decimal("0.20")
    assert priced.unpriced == ()
    rate_without_figure = estimate_monthly_cost(
        _telemetry(), _policy(snapshot_gb_month_rate="0.05"), now=early
    )
    assert rate_without_figure.unpriced == ("egress", "snapshot")


def test_billing_estimate_reproduces_adr_figures_from_injected_assumptions() -> None:
    """Feeding the ADR-0013 D4 assumptions reproduces its published estimates."""
    reference = _policy(
        volume_gb_month_rate="0.15",
        stopped_rootfs_gb_month_rate="0.15",
        running_hour_rate="0.0082",
    )
    now = datetime(2026, 9, 1, 6, tzinfo=UTC)
    reference_running_hours = 722  # the ADR's continuously-started month

    fully_stopped = estimate_monthly_cost(_telemetry(), reference, now=now)
    continuous = estimate_monthly_cost(
        _telemetry(
            stopped_rootfs_gb=0,
            running_seconds_injected=reference_running_hours * 3600,
        ),
        reference,
        now=now,
    )

    assert fully_stopped.estimated_month == Decimal("0.90")
    assert abs(continuous.estimated_month - Decimal("6.67")) <= Decimal("0.01")


def test_budget_departure_fires_at_equal_and_above_only() -> None:
    """The departure alarm compares Decimal estimate >= Decimal budget."""
    telemetry = _telemetry()
    now = datetime(2026, 9, 1, 6, tzinfo=UTC)
    exact = Decimal("1.10")

    def alerts_for(budget: Decimal) -> tuple[Alert, ...]:
        policy = _policy(monthly_budget=str(budget))
        estimate = estimate_monthly_cost(telemetry, policy, now=now)
        assert estimate.estimated_month == exact
        return evaluate_alerts((), estimate, policy)

    assert alerts_for(exact) == (
        Alert(AlertKind.MONTHLY_BUDGET_DEPARTURE, "fleet", None, "1.10", "1.10"),
    )
    assert alerts_for(exact + Decimal("0.01")) == ()
    assert alerts_for(exact - Decimal("0.01")) == (
        Alert(AlertKind.MONTHLY_BUDGET_DEPARTURE, "fleet", None, "1.10", "1.09"),
    )


def test_alert_vocabulary_is_exactly_five_kinds_and_each_condition_maps_to_one() -> (
    None
):
    """Alerts are a closed vocabulary keyed by identifiers only."""
    policy = _policy()
    estimate = estimate_monthly_cost(_telemetry(), policy, now=_NOW)
    divergences = (
        Divergence(
            DivergenceKind.ORPHAN_RESOURCE,
            Disposition.REPORTED,
            "fly-orphan",
            ResourceClass.VOLUME,
            None,
            None,
        ),
        Divergence(
            DivergenceKind.DUPLICATE_RESOURCE,
            Disposition.REPORTED,
            "fly-dup",
            ResourceClass.MACHINE,
            None,
            None,
        ),
        Divergence(
            DivergenceKind.MISSING_RESOURCE,
            Disposition.REPORTED,
            "fly-missing",
            ResourceClass.VOLUME,
            "job-missing",
            None,
        ),
        Divergence(
            DivergenceKind.UNCONFIRMED_DELETION,
            Disposition.REPORTED,
            "fly-young",
            None,
            "job-young",
            1799,
        ),
        Divergence(
            DivergenceKind.STUCK_DELETION,
            Disposition.REPAIRED,
            "fly-stuck",
            None,
            "job-stuck",
            1800,
        ),
        Divergence(
            DivergenceKind.CONTINUOUS_RUNNING,
            Disposition.REPAIRED,
            "fly-hot",
            ResourceClass.MACHINE,
            "job-hot",
            7200,
        ),
    )

    alerts = evaluate_alerts(divergences, estimate, policy)

    assert set(AlertKind) == {
        "duplicate_resource",
        "orphan_resource",
        "stuck_deletion",
        "continuous_running",
        "monthly_budget_departure",
    }
    assert alerts == (
        Alert(AlertKind.CONTINUOUS_RUNNING, "fly-hot", 7200, "7200", "7200"),
        Alert(AlertKind.DUPLICATE_RESOURCE, "fly-dup", None, None, None),
        Alert(AlertKind.ORPHAN_RESOURCE, "fly-orphan", None, None, None),
        Alert(AlertKind.STUCK_DELETION, "job-stuck", 1800, "1800", "1800"),
    )
    assert set(DivergenceKind) == {
        "orphan_resource",
        "missing_resource",
        "duplicate_resource",
        "unconfirmed_deletion",
        "stuck_deletion",
        "continuous_running",
    }
    assert set(Disposition) == {"reported", "repaired"}


def test_review_checkpoint_triggers_at_each_operator_threshold_and_manual_flag() -> (
    None
):
    """ADR-0013 Decision 7 triggers fire at operator thresholds, not ADR defaults."""
    policy = _policy()

    def review(
        telemetry: FleetTelemetry,
        months: tuple[bool, ...] = (),
        *,
        changed: bool = False,
    ) -> tuple[ReviewTrigger, ...]:
        return evaluate_review_checkpoint(
            telemetry,
            months,
            confidential_compute_changed=changed,
            policy=policy,
        )

    assert review(_telemetry(activated_allocations=9)) == ()
    assert review(_telemetry(activated_allocations=10)) == (
        ReviewTrigger.ACTIVATED_VAULTS,
    )
    assert review(_telemetry(provisioned_volumes=19)) == ()
    assert review(_telemetry(provisioned_volumes=20)) == (
        ReviewTrigger.PROVISIONED_VOLUMES,
    )
    assert review(_telemetry(), (True,)) == ()
    assert review(_telemetry(), (True, False, True)) == ()
    assert review(_telemetry(), (True, True)) == (ReviewTrigger.MONTHS_OVER_BUDGET,)
    assert review(_telemetry(), changed=True) == (
        ReviewTrigger.CONFIDENTIAL_COMPUTE_CHANGE,
    )
    assert review(
        _telemetry(activated_allocations=10, provisioned_volumes=20),
        (True, True),
        changed=True,
    ) == (
        ReviewTrigger.ACTIVATED_VAULTS,
        ReviewTrigger.PROVISIONED_VOLUMES,
        ReviewTrigger.CONFIDENTIAL_COMPUTE_CHANGE,
        ReviewTrigger.MONTHS_OVER_BUDGET,
    )
    assert set(ReviewTrigger) == {
        "activated_vaults",
        "provisioned_volumes",
        "confidential_compute_change",
        "months_over_budget",
    }


def test_unknown_rootfs_sizes_are_listed_as_unpriced_not_zeroed() -> None:
    """A Machine the provider did not size is reported, never counted as free."""
    policy = _policy()
    now = datetime(2026, 9, 1, 6, tzinfo=UTC)

    sized = estimate_monthly_cost(_telemetry(), policy, now=now)
    partly_unknown = estimate_monthly_cost(
        _telemetry(machines_without_rootfs_size=2), policy, now=now
    )

    assert sized.unpriced == ("egress", "snapshot")
    assert partly_unknown.unpriced == ("egress", "snapshot", "stopped_rootfs")
    assert partly_unknown.components["stopped_rootfs"] == Decimal("0.10")
    assert partly_unknown.estimated_month == sized.estimated_month


def test_a_closed_month_estimate_uses_its_sampled_seconds_without_projection() -> None:
    """Recording a closed month must not extrapolate its complete sample."""
    policy = _policy()
    now = datetime(2026, 9, 1, 2, tzinfo=UTC)
    sampled = _telemetry(running_machine_seconds_fleet=36000)

    closed = estimate_monthly_cost(sampled, policy, now=now, month="2026-08")
    live = estimate_monthly_cost(sampled, policy, now=now)
    injected = estimate_monthly_cost(
        _telemetry(running_machine_seconds_fleet=36000, running_seconds_injected=7200),
        policy,
        now=now,
        month="2026-08",
    )

    assert closed.running_basis is RunningBasis.CLOSED_MONTH
    assert closed.components["running"] == Decimal("0.10")
    assert live.running_basis is RunningBasis.MONTH_TO_DATE
    assert injected.running_basis is RunningBasis.INJECTED
    assert injected.components["running"] == Decimal("0.02")
