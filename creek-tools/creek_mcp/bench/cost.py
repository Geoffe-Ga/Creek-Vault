"""Per-account cost **model**: USD per account-month from an operator price sheet.

This is arithmetic, not a measurement, and it says so: every
:class:`CostEstimate` carries ``kind="model"``. It makes no network request;
prices come only from a JSON sheet the operator supplies, dated by its
``observed_on`` field, and a sheet that is undated, older than
:data:`MAX_PRICE_AGE_DAYS`, or dated in the future is refused.

**Formula.** One managed vault is one Fly Machine with one volume. Over a
month in which the machine runs for a fraction *duty* of the time::

    machine_monthly * duty            # compute is billed while running
    + volume_gb * volume_gb_month     # a volume is billed whether or not it runs
    + (1 - duty) * rootfs_gb * rootfs_gb_month   # a stopped machine's rootfs

rounded half-up to the cent. The allocation defaults are read from
:class:`creek_mcp.provisioning.fly.FlyProviderPolicy` itself, so the model
tracks what the provisioning driver actually requests; the operator can
override any of them.

**Duty.** The operator gives a low/high band. Optionally, an
:class:`AllowanceUse` derives the duty implied by a monthly reflection
allowance: each reflection keeps the machine up for its own latency plus the
idle linger before auto-stop, against :data:`HOURS_PER_BILLING_MONTH` billable
hours.
"""

import dataclasses
import json
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    model_validator,
)

from creek_mcp.provisioning.fly import FlyProviderPolicy

MAX_PRICE_AGE_DAYS: Final[int] = 90
"""The oldest price observation the model will use, in days."""

HOURS_PER_BILLING_MONTH: Final[int] = 730
"""Fly's billing month: 730 hours (365 days x 24 h / 12 months)."""

_SECONDS_PER_HOUR: Final[int] = 3600
_CENT: Final[Decimal] = Decimal("0.01")
_DUTY_QUANTUM: Final[Decimal] = Decimal("0.000001")
"""Precision a duty fraction is reported at; arithmetic uses full precision."""

_ALLOCATION_KEY_PATTERN: Final[str] = r"^(shared|performance)-[0-9]+x-[0-9]+mb$"
_SOURCE_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9.-]{0,63}$"

_NOT_FOUND: Final[str] = "price sheet not found"
_NOT_JSON: Final[str] = "price sheet is invalid: not JSON"
_STALE: Final[str] = "price sheet is stale: observed_on is too old"
_FUTURE: Final[str] = "price sheet is from the future: observed_on is after today"
_BAD_BAND: Final[str] = "duty band must satisfy 0 <= low <= high <= 1"

AllocationKey = Annotated[str, Field(pattern=_ALLOCATION_KEY_PATTERN)]
"""``<cpu_kind>-<cpus>x-<memory_mb>mb``, e.g. ``shared-1x-1024mb``."""

NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
Fraction = Annotated[Decimal, Field(ge=0, le=1)]


class PriceSheetError(ValueError):
    """The price sheet is missing, malformed, undated, stale, or incomplete."""


class PriceSheet(BaseModel):
    """Operator-observed prices, in USD.

    Attributes:
        observed_on: When these prices were read; required.
        currency: Always ``USD``.
        source: A short id for where they were read (``fly-invoice-2026-09``).
        machine_monthly_usd: Full-month price per allocation key.
        volume_gb_month_usd: Volume storage price per GB-month.
        rootfs_gb_month_usd: Stopped-machine rootfs price per GB-month.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_on: date
    currency: Literal["USD"]
    source: Annotated[str, Field(pattern=_SOURCE_PATTERN)]
    machine_monthly_usd: dict[AllocationKey, NonNegativeDecimal]
    volume_gb_month_usd: NonNegativeDecimal
    rootfs_gb_month_usd: NonNegativeDecimal


class Allocation(BaseModel):
    """One managed vault's Fly allocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_kind: Literal["shared", "performance"]
    cpus: PositiveInt
    memory_mb: PositiveInt
    rootfs_gb: PositiveInt
    volume_gb: PositiveInt

    @classmethod
    def from_fly_defaults(cls, overrides: Mapping[str, object] | None = None) -> Self:
        """Return the provisioning driver's default allocation, overridden.

        Args:
            overrides: Field values replacing the defaults, validated.

        Returns:
            The allocation.
        """
        defaults = {
            field.name: field.default
            for field in dataclasses.fields(FlyProviderPolicy)
            if field.default is not dataclasses.MISSING
        }
        values: dict[str, object] = {
            "cpu_kind": defaults["cpu_kind"],
            "cpus": defaults["cpus"],
            "memory_mb": defaults["memory_mb"],
            "rootfs_gb": defaults["rootfs_size_gb"],
            "volume_gb": defaults["volume_size_gb"],
        }
        values.update(overrides or {})
        return cls.model_validate(values)

    @property
    def key(self) -> str:
        """The price-sheet key for this allocation."""
        return f"{self.cpu_kind}-{self.cpus}x-{self.memory_mb}mb"


class DutyBand(BaseModel):
    """The low and high fraction of the month the machine runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: Fraction
    high: Fraction

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        """Refuse a band whose low end is above its high end."""
        if self.low > self.high:
            raise ValueError(_BAD_BAND)
        return self


class AllowanceUse(BaseModel):
    """How a monthly reflection allowance keeps a scale-to-zero machine up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reflections_per_month: NonNegativeInt
    seconds_per_reflection: NonNegativeDecimal
    linger_seconds: NonNegativeDecimal

    def duty(self) -> Decimal:
        """Return the running fraction of the month, at most one."""
        month = Decimal(HOURS_PER_BILLING_MONTH * _SECONDS_PER_HOUR)
        busy = self.reflections_per_month * (
            self.seconds_per_reflection + self.linger_seconds
        )
        return min(Decimal(1), busy / month)


class CostEstimate(BaseModel):
    """A modelled USD/account-month band. Never a benchmark result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["model"] = "model"
    observed_on: date
    allocation_key: AllocationKey
    duty_low: Fraction
    duty_high: Fraction
    low_usd: NonNegativeDecimal
    high_usd: NonNegativeDecimal
    allowance_duty: Fraction | None = None
    allowance_usd: NonNegativeDecimal | None = None


def _invalid(exc: ValidationError) -> PriceSheetError:
    """Name the offending fields only — never echo the operator's values."""
    fields = sorted(
        {".".join(str(part) for part in err["loc"]) for err in exc.errors()}
    )
    return PriceSheetError(f"price sheet is invalid: {', '.join(fields)}")


def load_price_sheet(path: Path, *, today: date) -> PriceSheet:
    """Read and date-check the operator's price sheet.

    Args:
        path: The JSON price sheet.
        today: The date the estimate is made on.

    Returns:
        The validated sheet.

    Raises:
        PriceSheetError: When the sheet is missing, malformed, undated,
            older than :data:`MAX_PRICE_AGE_DAYS`, or dated after *today*.
    """
    if not path.is_file():
        raise PriceSheetError(_NOT_FOUND)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PriceSheetError(_NOT_JSON) from exc
    try:
        sheet = PriceSheet.model_validate(raw)
    except ValidationError as exc:
        raise _invalid(exc) from exc
    age = (today - sheet.observed_on).days
    if age < 0:
        raise PriceSheetError(_FUTURE)
    if age > MAX_PRICE_AGE_DAYS:
        raise PriceSheetError(_STALE)
    return sheet


def _monthly(
    sheet: PriceSheet, machine: Decimal, allocation: Allocation, duty: Decimal
) -> Decimal:
    """Return the unrounded monthly cost at *duty*."""
    volume = allocation.volume_gb * sheet.volume_gb_month_usd
    rootfs = (1 - duty) * allocation.rootfs_gb * sheet.rootfs_gb_month_usd
    return machine * duty + volume + rootfs


def _cents(amount: Decimal) -> Decimal:
    """Round half-up to the cent."""
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def cost_bands(
    sheet: PriceSheet,
    allocation: Allocation,
    band: DutyBand,
    *,
    allowance: AllowanceUse | None = None,
) -> CostEstimate:
    """Model USD per account-month at the band's ends (and the allowance's duty).

    Args:
        sheet: Dated operator prices.
        allocation: The per-account machine allocation.
        band: Low and high running fractions of the month.
        allowance: Optional reflection allowance to derive a duty from.

    Returns:
        The modelled estimate.

    Raises:
        PriceSheetError: When the sheet has no price for *allocation*.
    """
    machine = sheet.machine_monthly_usd.get(allocation.key)
    if machine is None:
        msg = f"price sheet has no price for allocation {allocation.key}"
        raise PriceSheetError(msg)
    allowance_duty = allowance.duty() if allowance is not None else None
    return CostEstimate(
        observed_on=sheet.observed_on,
        allocation_key=allocation.key,
        duty_low=band.low,
        duty_high=band.high,
        low_usd=_cents(_monthly(sheet, machine, allocation, band.low)),
        high_usd=_cents(_monthly(sheet, machine, allocation, band.high)),
        allowance_duty=(
            None
            if allowance_duty is None
            else allowance_duty.quantize(_DUTY_QUANTUM, rounding=ROUND_HALF_UP)
        ),
        allowance_usd=(
            None
            if allowance_duty is None
            else _cents(_monthly(sheet, machine, allocation, allowance_duty))
        ),
    )
