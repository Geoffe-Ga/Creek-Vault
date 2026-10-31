"""Operator-injected fleet budget policy, cost estimate, alerts, and review (#1769).

ADR-0013 Decision 4 makes the cost model a guardrail, not code: this module
carries no rate, budget, duration, or review threshold of its own.  Every
figure arrives from the operator's TOML policy file, parsed with
``parse_float=Decimal`` so money never passes through binary floating point,
and the estimate lists every unpriced or unknown input instead of zeroing it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from creek_mcp.bench.cost import MAX_PRICE_AGE_DAYS
from creek_mcp.provisioning.models import (
    Disposition,
    DivergenceKind,
    ResourceClass,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from creek_mcp.provisioning.models import Divergence, FleetTelemetry

_BYTES_PER_GB: Final[Decimal] = Decimal(1024**3)
_SECONDS_PER_HOUR: Final[Decimal] = Decimal(3600)
_CENT: Final[Decimal] = Decimal("0.01")
_PROJECTION_MIN_ELAPSED: Final[timedelta] = timedelta(days=1)
"""Compute is projected to the calendar month only after this much has elapsed."""
_MONTH_ROLLOVER: Final[timedelta] = timedelta(days=32)
_MONEY_FIELDS: Final[tuple[str, ...]] = (
    "monthly_budget",
    "volume_gb_month_rate",
    "stopped_rootfs_gb_month_rate",
    "running_hour_rate",
    "snapshot_gb_month_rate",
    "egress_gb_rate",
    "pool_input_mtoken_rate",
    "pool_output_mtoken_rate",
)
_POOL_RATE_FIELDS: Final[tuple[str, ...]] = (
    "pool_input_mtoken_rate",
    "pool_output_mtoken_rate",
)
"""The pool's metered vendor rates, per million tokens (ADR-0016 D3.6)."""
_TOKENS_PER_MTOKEN: Final[Decimal] = Decimal(1_000_000)
_POOL_COMPONENT: Final[str] = "pool"
_POOL_UNDATED: Final[str] = (
    "pool_rate_observed_on is required when a pool rate is priced"
)
_POOL_STALE: Final[str] = "pool price sheet is stale: pool_rate_observed_on is too old"
_POOL_FUTURE: Final[str] = (
    "pool price sheet is from the future: pool_rate_observed_on is after today"
)
_DURATION_KEYS: Final[dict[str, str]] = {
    "max_continuous_running": "max_continuous_running_seconds",
    "stuck_deletion_after": "stuck_deletion_seconds",
}
_COUNT_FIELDS: Final[tuple[str, ...]] = (
    "review_activated_vaults",
    "review_provisioned_volumes",
    "review_months_over_budget",
)
_POLICY_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("budget", ""),
    ("policy", ""),
    ("review", "review_"),
)
_USAGE_KEYS: Final[tuple[str, ...]] = (
    "snapshot_bytes",
    "egress_bytes",
    "running_seconds",
    "pool_input_tokens",
    "pool_output_tokens",
)
FLEET_SUBJECT: Final[str] = "fleet"
"""The subject an alert carries when it is about the fleet, not a resource."""


@dataclass(frozen=True, slots=True)
class FleetPolicy:
    """Every operator value the fleet tooling needs; nothing has a default."""

    currency: str
    monthly_budget: Decimal
    volume_gb_month_rate: Decimal
    stopped_rootfs_gb_month_rate: Decimal
    running_hour_rate: Decimal
    snapshot_gb_month_rate: Decimal | None
    egress_gb_rate: Decimal | None
    pool_input_mtoken_rate: Decimal | None
    pool_output_mtoken_rate: Decimal | None
    pool_rate_observed_on: date | None
    max_continuous_running: timedelta
    stuck_deletion_after: timedelta
    review_activated_vaults: int
    review_provisioned_volumes: int
    review_months_over_budget: int

    def __post_init__(self) -> None:
        """Reject blank currency and non-positive money, durations, or counts."""
        if not self.currency.strip():
            raise ValueError("currency must not be blank")
        for name in _MONEY_FIELDS:
            money: Decimal | None = getattr(self, name)
            if money is not None and money <= 0:
                raise ValueError(f"{name} must be positive")
        for name, key in _DURATION_KEYS.items():
            duration: timedelta = getattr(self, name)
            if duration <= timedelta(0):
                raise ValueError(f"{key} must be positive")
        for name in _COUNT_FIELDS:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> FleetPolicy:
        """Build a policy from flat operator keys, naming any missing one."""
        return cls(
            currency=_text(mapping, "currency"),
            monthly_budget=_money(mapping, "monthly_budget"),
            volume_gb_month_rate=_money(mapping, "volume_gb_month_rate"),
            stopped_rootfs_gb_month_rate=_money(
                mapping, "stopped_rootfs_gb_month_rate"
            ),
            running_hour_rate=_money(mapping, "running_hour_rate"),
            snapshot_gb_month_rate=_optional_money(mapping, "snapshot_gb_month_rate"),
            egress_gb_rate=_optional_money(mapping, "egress_gb_rate"),
            pool_input_mtoken_rate=_optional_money(mapping, "pool_input_mtoken_rate"),
            pool_output_mtoken_rate=_optional_money(mapping, "pool_output_mtoken_rate"),
            pool_rate_observed_on=_optional_date(mapping, "pool_rate_observed_on"),
            max_continuous_running=_duration(mapping, "max_continuous_running_seconds"),
            stuck_deletion_after=_duration(mapping, "stuck_deletion_seconds"),
            review_activated_vaults=_count(mapping, "review_activated_vaults"),
            review_provisioned_volumes=_count(mapping, "review_provisioned_volumes"),
            review_months_over_budget=_count(mapping, "review_months_over_budget"),
        )

    @classmethod
    def from_toml(cls, path: Path, *, today: date | None = None) -> FleetPolicy:
        """Load only the policy from an operator TOML file.

        Args:
            path: The operator policy file.
            today: The date the load is dated against; today in UTC when
                omitted.

        Returns:
            The policy.
        """
        return load_policy_file(path, today=today).policy


@dataclass(frozen=True, slots=True)
class InjectedUsage:
    """Invoice figures the provider API does not expose, copied in by the operator."""

    snapshot_bytes: int | None
    egress_bytes: int | None
    running_seconds: int | None
    pool_input_tokens: int | None = None
    pool_output_tokens: int | None = None
    """Pool tokens read off the vendor's usage page (ADR-0016 Decision 4).

    Optional like every other injected figure: absent leaves the pool cost
    line unpriced and listed, never zero.
    """


@dataclass(frozen=True, slots=True)
class PolicyFile:
    """The parsed operator policy file: required policy plus optional usage."""

    policy: FleetPolicy
    usage: InjectedUsage


def load_policy_file(path: Path, *, today: date | None = None) -> PolicyFile:
    """Parse ``[budget]``, ``[policy]``, ``[review]`` and optional ``[usage]``.

    Floats are parsed straight into ``Decimal`` so a rate written as ``0.1``
    is exactly ``Decimal("0.1")``.  Errors name the offending key and never
    echo file content.

    A priced pool line is dated: ADR-0016 Decision 4 makes the pool a metered
    vendor line, and R6 prices it from a dated sheet rather than arithmetic
    alone, so a pool rate without ``pool_rate_observed_on``, or one older than
    :data:`~creek_mcp.bench.cost.MAX_PRICE_AGE_DAYS` or dated after *today*,
    is refused exactly as ``bench.sh cost`` refuses its own sheet (T16).

    Args:
        path: The operator policy file.
        today: The date the sheet is dated against; today in UTC when omitted.

    Returns:
        The parsed policy and injected usage.

    Raises:
        ValueError: When the file is unreadable, is not valid TOML, names an
            invalid key, or carries an undated, stale or future pool sheet.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except OSError as exc:
        raise ValueError("policy file is unreadable") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("policy file is not valid TOML") from exc
    flat: dict[str, object] = {}
    for section, prefix in _POLICY_SECTIONS:
        table = _table(document, section)
        flat.update({f"{prefix}{key}": value for key, value in table.items()})
    usage = _table(document, "usage")
    policy = FleetPolicy.from_mapping(flat)
    _require_fresh_pool_sheet(policy, today or datetime.now(tz=UTC).date())
    return PolicyFile(
        policy=policy,
        usage=InjectedUsage(
            **{key: _optional_count(usage, key) for key in _USAGE_KEYS}
        ),
    )


def _require_fresh_pool_sheet(policy: FleetPolicy, today: date) -> None:
    """Refuse an undated, stale or future pool price sheet.

    Args:
        policy: The parsed policy.
        today: The date to measure the sheet's age against.

    Raises:
        ValueError: When a pool rate is priced without a usable date.
    """
    if not any(getattr(policy, name) is not None for name in _POOL_RATE_FIELDS):
        return
    observed = policy.pool_rate_observed_on
    if observed is None:
        raise ValueError(_POOL_UNDATED)
    age = (today - observed).days
    if age < 0:
        raise ValueError(_POOL_FUTURE)
    if age > MAX_PRICE_AGE_DAYS:
        raise ValueError(_POOL_STALE)


def _table(document: Mapping[str, object], section: str) -> Mapping[str, object]:
    """Return one optional TOML table, refusing a non-table value."""
    table = document.get(section, {})
    if not isinstance(table, dict):
        raise ValueError(f"policy [{section}] must be a table")
    return table


def _required(mapping: Mapping[str, object], key: str) -> object:
    """Return *key* or fail naming it."""
    if key not in mapping:
        raise ValueError(f"policy is missing {key}")
    return mapping[key]


def _text(mapping: Mapping[str, object], key: str) -> str:
    """Return one required string value."""
    value = _required(mapping, key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _decimal(value: object, key: str) -> Decimal:
    """Convert an exact operator amount; binary floats are refused by construction."""
    if isinstance(value, bool) or not isinstance(value, int | str | Decimal):
        raise ValueError(f"{key} must be a decimal string, integer, or TOML number")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{key} is not a decimal amount") from exc
    if not amount.is_finite():
        raise ValueError(f"{key} must be a finite decimal amount")
    return amount


def _money(mapping: Mapping[str, object], key: str) -> Decimal:
    """Return one required exact amount."""
    return _decimal(_required(mapping, key), key)


def _optional_money(mapping: Mapping[str, object], key: str) -> Decimal | None:
    """Return one optional exact amount; absent or null means unpriced."""
    value = mapping.get(key)
    return None if value is None else _decimal(value, key)


def _count(mapping: Mapping[str, object], key: str) -> int:
    """Return one required integer."""
    value = _required(mapping, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_count(mapping: Mapping[str, object], key: str) -> int | None:
    """Return one optional non-negative integer."""
    if mapping.get(key) is None:
        return None
    value = _count(mapping, key)
    if value < 0:
        raise ValueError(f"{key} must not be negative")
    return value


def _optional_date(mapping: Mapping[str, object], key: str) -> date | None:
    """Return one optional TOML date; absent or null means undated."""
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{key} must be a plain TOML date")
    return value


def _duration(mapping: Mapping[str, object], key: str) -> timedelta:
    """Return one required whole-seconds duration."""
    return timedelta(seconds=_count(mapping, key))


@unique
class RunningBasis(StrEnum):
    """Which running-hours figure the estimate was computed from."""

    INJECTED = "injected"
    PROJECTED = "projected"
    MONTH_TO_DATE = "month_to_date"
    CLOSED_MONTH = "closed_month"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A month estimate whose unpriced inputs are listed, never silently zero."""

    currency: str
    estimated_month: Decimal
    components: Mapping[str, Decimal]
    unpriced: tuple[str, ...]
    running_basis: RunningBasis


def _cents(value: Decimal) -> Decimal:
    """Round one amount to the operator currency's cents."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _gb(size_bytes: int) -> Decimal:
    """Convert bytes to the GB unit the operator rates are quoted in."""
    return Decimal(size_bytes) / _BYTES_PER_GB


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return the UTC calendar-month start and next-month start around *now*."""
    start = now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return start, (start + _MONTH_ROLLOVER).replace(day=1)


def _running_seconds(
    telemetry: FleetTelemetry,
    now: datetime,
    month: str | None,
) -> tuple[Decimal, RunningBasis]:
    """Pick injected, closed-month, projected, or month-to-date running seconds."""
    if telemetry.running_seconds_injected is not None:
        return Decimal(telemetry.running_seconds_injected), RunningBasis.INJECTED
    sampled = Decimal(telemetry.running_machine_seconds_fleet)
    if month is not None:
        return sampled, RunningBasis.CLOSED_MONTH
    start, end = _month_bounds(now)
    elapsed = now - start
    if elapsed < _PROJECTION_MIN_ELAPSED:
        return sampled, RunningBasis.MONTH_TO_DATE
    scale = Decimal((end - start).total_seconds()) / Decimal(elapsed.total_seconds())
    return sampled * scale, RunningBasis.PROJECTED


def estimate_monthly_cost(
    telemetry: FleetTelemetry,
    policy: FleetPolicy,
    *,
    now: datetime,
    month: str | None = None,
) -> CostEstimate:
    """Estimate one calendar month from telemetry and the injected policy only.

    With *month* set (``YYYY-MM``, a closed month) the sampled running seconds
    in *telemetry* are taken as that month's complete figure and never
    projected.  Machines whose root filesystem size the provider did not
    report make ``stopped_rootfs`` an unpriced input as well as a component.
    """
    components: dict[str, Decimal] = {
        "volume": _gb(telemetry.volume_bytes) * policy.volume_gb_month_rate,
        "stopped_rootfs": (
            Decimal(telemetry.stopped_rootfs_gb) * policy.stopped_rootfs_gb_month_rate
        ),
    }
    seconds, basis = _running_seconds(telemetry, now, month)
    components["running"] = seconds / _SECONDS_PER_HOUR * policy.running_hour_rate
    unpriced: list[str] = []
    if telemetry.machines_without_rootfs_size:
        unpriced.append("stopped_rootfs")
    priced_inputs = (
        ("egress", telemetry.egress_bytes, policy.egress_gb_rate),
        ("snapshot", telemetry.snapshot_bytes, policy.snapshot_gb_month_rate),
    )
    for name, size_bytes, rate in priced_inputs:
        if size_bytes is None or rate is None:
            unpriced.append(name)
        else:
            components[name] = _gb(size_bytes) * rate
    pool = _pool_component(telemetry, policy)
    if pool is None:
        unpriced.append(_POOL_COMPONENT)
    else:
        components[_POOL_COMPONENT] = pool
    total = sum(components.values(), Decimal(0))
    return CostEstimate(
        currency=policy.currency,
        estimated_month=_cents(total),
        components={name: _cents(value) for name, value in components.items()},
        unpriced=tuple(sorted(unpriced)),
        running_basis=basis,
    )


def _pool_component(
    telemetry: FleetTelemetry,
    policy: FleetPolicy,
) -> Decimal | None:
    """Price the pool's metered vendor line, or report it unpriced.

    ADR-0016 Decision 4 keeps the pool a separate line: it is never folded
    into the pilot's storage-era monthly figure.  The free allowance is not
    treated as a budget, so an allowance-covered month still prices at the
    sheet's rates.  Any missing rate or token count leaves the line unpriced
    and listed rather than silently zero.

    Args:
        telemetry: The pass's measurements, carrying the injected token totals.
        policy: The dated operator policy.

    Returns:
        The month's pool cost, or ``None`` when an input is missing.
    """
    priced = (
        (telemetry.pool_input_tokens, policy.pool_input_mtoken_rate),
        (telemetry.pool_output_tokens, policy.pool_output_mtoken_rate),
    )
    if any(tokens is None or rate is None for tokens, rate in priced):
        return None
    return sum(
        (
            Decimal(tokens or 0) / _TOKENS_PER_MTOKEN * (rate or Decimal(0))
            for tokens, rate in priced
        ),
        Decimal(0),
    )


@unique
class AlertKind(StrEnum):
    """The closed operator alert vocabulary (ADR-0013 Decision 4)."""

    DUPLICATE_RESOURCE = "duplicate_resource"
    ORPHAN_RESOURCE = "orphan_resource"
    STUCK_DELETION = "stuck_deletion"
    CONTINUOUS_RUNNING = "continuous_running"
    INCOMPLETE_CREATE = "incomplete_create"
    MONTHLY_BUDGET_DEPARTURE = "monthly_budget_departure"
    POOL_CREDENTIAL_ORPHAN = "pool_credential_orphan"
    # The organization admin key (ADR-0016 Decision 3.4, #1930).  Its failure
    # modes are *not* ``POOL_CREDENTIAL_ORPHAN``: that one names a leftover
    # per-activation key, which costs money until it is revoked, while these
    # name the one credential that can re-cap or delete every activation's
    # key, whose loss stops the whole lifecycle rather than spending on it.
    POOL_ADMIN_KEY_UNUSABLE = "pool_admin_key_unusable"
    POOL_ADMIN_KEY_REJECTED = "pool_admin_key_rejected"
    POOL_ADMIN_KEY_NEAR_EXPIRY = "pool_admin_key_near_expiry"
    # The Fly org token's operator-recorded expiry (#1867, ADR-0013 amendment):
    # within the warning window, already expired, or never recorded.
    PROVIDER_CREDENTIAL_EXPIRING = "provider_credential_expiring"
    # The Fly provider refused the pilot's org token with a 401/403 (#1866):
    # the pilot is degraded *now*, unlike an expiry warning ahead of time.
    PROVIDER_CREDENTIAL_REJECTED = "provider_credential_rejected"


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert keyed by a provider allocation id, a job id, or ``fleet``."""

    kind: AlertKind
    subject: str
    age_seconds: int | None
    measured: str | None
    threshold: str | None


_PER_RESOURCE_ALERTS: Final[dict[DivergenceKind, AlertKind]] = {
    DivergenceKind.ORPHAN_RESOURCE: AlertKind.ORPHAN_RESOURCE,
    DivergenceKind.DUPLICATE_RESOURCE: AlertKind.DUPLICATE_RESOURCE,
    DivergenceKind.INCOMPLETE_CREATE: AlertKind.INCOMPLETE_CREATE,
}


def _narrowed(kind: AlertKind, divergence: Divergence) -> AlertKind:
    """Name a surviving pool credential as its own kind (ADR-0016 D1.12).

    A credential left behind by a deletion is operationally unlike an orphaned
    volume: it is still spendable up to its cap until it is revoked, so it gets
    its own alert kind rather than hiding inside ``orphan_resource`` (T11).

    Args:
        kind: The alert kind the divergence's own kind maps to.
        divergence: The divergence being alerted on.

    Returns:
        The narrowed kind, or *kind* unchanged.
    """
    if (
        kind is AlertKind.ORPHAN_RESOURCE
        and divergence.resource_class is ResourceClass.CREDENTIAL
    ):
        return AlertKind.POOL_CREDENTIAL_ORPHAN
    return kind


def _alert_for(divergence: Divergence, policy: FleetPolicy) -> Alert | None:
    """Map one divergence to its alert, or None for report-only kinds."""
    if (
        divergence.kind is DivergenceKind.INCOMPLETE_CREATE
        and divergence.disposition is Disposition.REPAIRED
    ):
        return None
    resource_kind = _PER_RESOURCE_ALERTS.get(divergence.kind)
    if resource_kind is not None:
        subject = divergence.provider_allocation_id or FLEET_SUBJECT
        return Alert(_narrowed(resource_kind, divergence), subject, None, None, None)
    if divergence.kind is DivergenceKind.STUCK_DELETION:
        kind, subject = AlertKind.STUCK_DELETION, divergence.job_id or FLEET_SUBJECT
        threshold = policy.stuck_deletion_after
    elif divergence.kind is DivergenceKind.CONTINUOUS_RUNNING:
        kind = AlertKind.CONTINUOUS_RUNNING
        subject = divergence.provider_allocation_id or FLEET_SUBJECT
        threshold = policy.max_continuous_running
    else:
        return None
    age = divergence.age_seconds
    return Alert(
        kind,
        subject,
        age,
        None if age is None else str(age),
        str(int(threshold.total_seconds())),
    )


def evaluate_alerts(
    divergences: Sequence[Divergence],
    estimate: CostEstimate,
    policy: FleetPolicy,
) -> tuple[Alert, ...]:
    """Return the sorted alerts for a pass; budget departure fires at ``>=``."""
    alerts = [
        alert
        for divergence in divergences
        if (alert := _alert_for(divergence, policy)) is not None
    ]
    if estimate.estimated_month >= policy.monthly_budget:
        alerts.append(
            Alert(
                AlertKind.MONTHLY_BUDGET_DEPARTURE,
                FLEET_SUBJECT,
                None,
                str(estimate.estimated_month),
                str(policy.monthly_budget),
            )
        )
    return tuple(sorted(alerts, key=lambda alert: (alert.kind.value, alert.subject)))


@unique
class ReviewTrigger(StrEnum):
    """ADR-0013 Decision 7 review-checkpoint triggers."""

    ACTIVATED_VAULTS = "activated_vaults"
    PROVISIONED_VOLUMES = "provisioned_volumes"
    CONFIDENTIAL_COMPUTE_CHANGE = "confidential_compute_change"
    MONTHS_OVER_BUDGET = "months_over_budget"


def evaluate_review_checkpoint(
    telemetry: FleetTelemetry,
    months_over_budget: Sequence[bool],
    *,
    confidential_compute_changed: bool,
    policy: FleetPolicy,
) -> tuple[ReviewTrigger, ...]:
    """Return every D7 trigger reached at the operator's thresholds."""
    window = policy.review_months_over_budget
    conditions = (
        (
            ReviewTrigger.ACTIVATED_VAULTS,
            telemetry.activated_allocations >= policy.review_activated_vaults,
        ),
        (
            ReviewTrigger.PROVISIONED_VOLUMES,
            telemetry.provisioned_volumes >= policy.review_provisioned_volumes,
        ),
        (ReviewTrigger.CONFIDENTIAL_COMPUTE_CHANGE, confidential_compute_changed),
        (
            ReviewTrigger.MONTHS_OVER_BUDGET,
            len(months_over_budget) >= window and all(months_over_budget[:window]),
        ),
    )
    return tuple(trigger for trigger, reached in conditions if reached)
