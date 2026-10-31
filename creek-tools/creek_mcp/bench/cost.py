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

**The metered pool (#1897).** ADR-0016 is ratified as O5, a vendor-attested
confidential pool billed per token, so a second cost shape lives here beside
the Machine-month one: :func:`metered_estimate` prices a reflection from the
sheet's ``metered_per_million`` block and reports cost per 1,000 reflections at
ADR-0016's two volumes, cost per account-month at the monthly reflection
allowance, and the Decision 3.4 vendor-key token caps. The two shapes share one
sheet, one staleness rule and one refusal style; a sheet may carry either
block, both, or neither.

**One rule governs every metered figure: only provider-reported counts may
price it.** The measured block from #1909 carries its own
:class:`~creek_mcp.bench.usage.TokenBasis`, and
:func:`~creek_mcp.provisioning.pool_bundle.measured_reflection_tokens` admits
only :attr:`~creek_mcp.bench.usage.TokenBasis.PROVIDER_REPORTED`. A fake run's
``byte_upper_bound`` bounds bytes, not price; ``unreported`` and
``not_measured`` are absences. Each is refused rather than estimated, because a
plausible figure quoted from a basis that cannot support it is worse than no
figure: someone budgets against it. The superseded 2,500 + 128 planning
assumption is never a fallback.

Metered amounts are rounded **up** to the micro-unit. Rounding a price down
understates a bill, and under-reporting the brakes is the failure this module
exists to prevent.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
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

from creek_mcp.bench.tokens import TokensPerReflection
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.provisioning.fly import FlyProviderPolicy
from creek_mcp.provisioning.pool_bundle import (
    measured_reflection_tokens,
    pool_cap_tokens,
)

MAX_PRICE_AGE_DAYS: Final[int] = 90
"""The oldest price observation the model will use, in days."""

HOURS_PER_BILLING_MONTH: Final[int] = 730
"""Fly's billing month: 730 hours (365 days x 24 h / 12 months)."""

DEFAULT_MONTHLY_REFLECTION_CAP: Final[int] = 20
"""The monthly charged-generation allowance a metered figure is priced at.

Mirrors adepthood's ``backend/src/services/usage.py`` ``DEFAULT_MONTHLY_CAP``,
which is shared across transcription, resonance passes and letters — so a
figure priced at the worst measured surface is the honest one. It is a default
here, overridable by flag, never a value this repo owns.
"""

PILOT_REFLECTIONS_PER_MONTH: Final[int] = 300
"""The release pilot ceiling: 15 live allocations at the default allowance.

ADR-0017 (owner decision R2-3) raised the cap from ADR-0016's five
allocations (100 reflections) to 15.
"""

PRODUCTION_REFLECTIONS_PER_MONTH: Final[int] = 10_000
"""ADR-0016's production volume: 500 accounts at the default allowance."""

DEFAULT_METERED_VOLUMES: Final[tuple[int, ...]] = (
    PILOT_REFLECTIONS_PER_MONTH,
    PRODUCTION_REFLECTIONS_PER_MONTH,
)
"""The two monthly volumes R6 requires a cost-per-1,000 figure at."""

_SECONDS_PER_HOUR: Final[int] = 3600
_CENT: Final[Decimal] = Decimal("0.01")
_MICRO: Final[Decimal] = Decimal("0.000001")
"""Quantum for metered amounts: a reflection can cost well under a cent."""

_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
_PER_THOUSAND: Final[int] = 1_000
_DUTY_QUANTUM: Final[Decimal] = Decimal("0.000001")
"""Precision a duty fraction is reported at; arithmetic uses full precision."""

_ALLOCATION_KEY_PATTERN: Final[str] = r"^(shared|performance)-[0-9]+x-[0-9]+mb$"
_SOURCE_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9.-]{0,63}$"
_MODEL_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._:/-]{0,63}$"
"""A model id as a catalogue names it: closed, so no free text can hide here."""

_NOT_FOUND: Final[str] = "price sheet not found"
_NOT_JSON: Final[str] = "price sheet is invalid: not JSON"
_STALE: Final[str] = "price sheet is stale: observed_on is too old"
_FUTURE: Final[str] = "price sheet is from the future: observed_on is after today"
_BAD_BAND: Final[str] = "duty band must satisfy 0 <= low <= high <= 1"
_WRONG_CURRENCY: Final[str] = (
    "price sheet currency does not match the requested currency"
)
_NO_MACHINE_PRICES: Final[str] = (
    "price sheet has no machine-month prices: machine_monthly_usd, "
    "volume_gb_month_usd, rootfs_gb_month_usd"
)
_NO_METERED_BLOCK: Final[str] = "price sheet has no metered block: metered_per_million"
_NO_METERED_MODEL: Final[str] = (
    "price sheet has no metered price for the requested model"
)

AllocationKey = Annotated[str, Field(pattern=_ALLOCATION_KEY_PATTERN)]
"""``<cpu_kind>-<cpus>x-<memory_mb>mb``, e.g. ``shared-1x-1024mb``."""

ModelId = Annotated[str, Field(pattern=_MODEL_ID_PATTERN)]
"""A metered model id, as the vendor's catalogue names it."""

Currency = Literal["USD", "EUR"]
"""The currencies a sheet may be quoted in.

ADR-0016's O5 candidates are priced in both, so a sheet must be able to say
which rather than have its euros read as dollars. Admitting a code is not
converting it: nothing here holds an exchange rate, and
:func:`metered_estimate` refuses a sheet whose currency is not the one asked
for.
"""

NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
Fraction = Annotated[Decimal, Field(ge=0, le=1)]


class PriceSheetError(ValueError):
    """The price sheet is missing, malformed, undated, stale, or incomplete."""


class MeteredPrices(BaseModel):
    """One model's metered prices, per million tokens, in the sheet's currency.

    Attributes:
        input_per_million: Price per million prompt tokens.
        output_per_million: Price per million generated tokens.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_million: NonNegativeDecimal
    output_per_million: NonNegativeDecimal


class PriceSheet(BaseModel):
    """Operator-observed prices, in the currency the sheet declares.

    Two independent blocks, because the two architectures are priced
    differently and one sheet may describe either or both: the Machine-month
    block (per-vault Fly compute and storage) and the metered block (ADR-0016's
    per-token pool). A sheet carrying neither is dated and well-formed and
    still prices nothing; the refusal then names the block that is missing,
    from :func:`cost_bands` or :func:`metered_estimate`.

    Attributes:
        observed_on: When these prices were read; required.
        currency: Which currency every amount below is quoted in.
        source: A short id for where they were read (``fly-invoice-2026-09``).
        machine_monthly_usd: Full-month price per allocation key; ``None`` on a
            metered-only sheet.
        volume_gb_month_usd: Volume storage price per GB-month, or ``None``.
        rootfs_gb_month_usd: Stopped-machine rootfs price per GB-month, or
            ``None``.
        metered_per_million: Per-million-token prices per model id, or ``None``
            on a Machine-month-only sheet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_on: date
    currency: Currency
    source: Annotated[str, Field(pattern=_SOURCE_PATTERN)]
    machine_monthly_usd: dict[AllocationKey, NonNegativeDecimal] | None = None
    volume_gb_month_usd: NonNegativeDecimal | None = None
    rootfs_gb_month_usd: NonNegativeDecimal | None = None
    metered_per_million: dict[ModelId, MeteredPrices] | None = None


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
    """A modelled USD/account-month band. Never a benchmark result.

    ``shape`` names which cost architecture this priced, so a reader of a
    written estimate never has to infer it from which fields are present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["model"] = "model"
    shape: Literal["machine_month"] = "machine_month"
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
    located = {".".join(str(part) for part in err["loc"]) for err in exc.errors()}
    fields = sorted(located - {""})
    if not fields:
        return PriceSheetError(_NOT_JSON)
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
        sheet = PriceSheet.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise _invalid(exc) from exc
    age = (today - sheet.observed_on).days
    if age < 0:
        raise PriceSheetError(_FUTURE)
    if age > MAX_PRICE_AGE_DAYS:
        raise PriceSheetError(_STALE)
    return sheet


def _machine_prices(
    sheet: PriceSheet, allocation: Allocation
) -> tuple[Decimal, Decimal, Decimal]:
    """Return the Machine-month prices for *allocation*, or refuse.

    Args:
        sheet: The dated sheet.
        allocation: The per-account machine allocation.

    Returns:
        ``(machine_monthly, volume_gb_month, rootfs_gb_month)``.

    Raises:
        PriceSheetError: When the sheet is not in USD, carries no
            Machine-month block, or prices no such allocation.
    """
    if sheet.currency != "USD":
        raise PriceSheetError(_WRONG_CURRENCY)
    machines = sheet.machine_monthly_usd
    volume = sheet.volume_gb_month_usd
    rootfs = sheet.rootfs_gb_month_usd
    if machines is None or volume is None or rootfs is None:
        raise PriceSheetError(_NO_MACHINE_PRICES)
    machine = machines.get(allocation.key)
    if machine is None:
        msg = f"price sheet has no price for allocation {allocation.key}"
        raise PriceSheetError(msg)
    return machine, volume, rootfs


def _monthly(
    prices: tuple[Decimal, Decimal, Decimal], allocation: Allocation, duty: Decimal
) -> Decimal:
    """Return the unrounded monthly cost at *duty*."""
    machine, volume_rate, rootfs_rate = prices
    volume = allocation.volume_gb * volume_rate
    rootfs = (1 - duty) * allocation.rootfs_gb * rootfs_rate
    return machine * duty + volume + rootfs


def _cents(amount: Decimal) -> Decimal:
    """Round half-up to the cent."""
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _micro_up(amount: Decimal) -> Decimal:
    """Round a metered amount **up** to the micro-unit.

    Never half-up: a price rounded down understates a bill, and the brakes
    this module reports are worth more than the last ten-thousandth of a cent.
    """
    return amount.quantize(_MICRO, rounding=ROUND_CEILING)


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
        PriceSheetError: When the sheet is not in USD, carries no Machine-month
            block, or has no price for *allocation*.
    """
    prices = _machine_prices(sheet, allocation)
    allowance_duty = allowance.duty() if allowance is not None else None
    return CostEstimate(
        observed_on=sheet.observed_on,
        allocation_key=allocation.key,
        duty_low=band.low,
        duty_high=band.high,
        low_usd=_cents(_monthly(prices, allocation, band.low)),
        high_usd=_cents(_monthly(prices, allocation, band.high)),
        allowance_duty=(
            None
            if allowance_duty is None
            else allowance_duty.quantize(_DUTY_QUANTUM, rounding=ROUND_HALF_UP)
        ),
        allowance_usd=(
            None
            if allowance_duty is None
            else _cents(_monthly(prices, allocation, allowance_duty))
        ),
    )


class MeteredVolume(BaseModel):
    """One monthly volume's metered figures.

    Attributes:
        reflections_per_month: The fleet-wide monthly volume priced.
        tokens_per_month: Prompt plus output tokens that volume consumes, at
            the measured per-reflection counts. Comparable with a vendor's
            free-tier grant, which is a grant and never a cap.
        cost_per_1000_reflections: R6's figure at this volume.
        cost_per_month: The whole volume's monthly bill.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reflections_per_month: PositiveInt
    tokens_per_month: NonNegativeInt
    cost_per_1000_reflections: NonNegativeDecimal
    cost_per_month: NonNegativeDecimal


class MeteredEstimate(BaseModel):
    """ADR-0016's per-token cost line, and the caps that bound it.

    ``kind`` stays ``model``: this is arithmetic over a measurement, and it
    must keep saying so. ``token_basis`` is the measurement's own basis, so
    the figure travels with the evidence that licenses it — always
    ``provider_reported``, because nothing else is constructible.

    Attributes:
        kind: Always ``model``.
        shape: Always ``metered_tokens``.
        observed_on: The sheet's observation date.
        source: The sheet's own source id.
        currency: The currency every amount here is in, as requested *and*
            as the sheet declares; a mismatch is refused, never converted.
        model_id: Which priced model these figures are for.
        token_basis: The measured block's basis.
        prompt_tokens_per_reflection: The worst measured prompt count.
        output_tokens_per_reflection: The worst measured output count.
        per_volume: One entry per monthly volume priced.
        monthly_reflection_cap: The per-account allowance priced.
        cost_per_account_month: One account's bill at that allowance.
        vendor_key_input_token_cap: Decision 3.4's input cap, from
            :func:`~creek_mcp.provisioning.pool_bundle.pool_cap_tokens`.
        vendor_key_output_token_cap: The same, for output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["model"] = "model"
    shape: Literal["metered_tokens"] = "metered_tokens"
    observed_on: date
    source: Annotated[str, Field(pattern=_SOURCE_PATTERN)]
    currency: Currency
    model_id: ModelId
    token_basis: TokenBasis
    prompt_tokens_per_reflection: PositiveInt
    output_tokens_per_reflection: PositiveInt
    per_volume: tuple[MeteredVolume, ...]
    monthly_reflection_cap: PositiveInt
    cost_per_account_month: NonNegativeDecimal
    vendor_key_input_token_cap: PositiveInt
    vendor_key_output_token_cap: PositiveInt


def _metered_prices(
    sheet: PriceSheet, model_id: str, currency: Currency
) -> MeteredPrices:
    """Return the metered prices for *model_id*, or refuse.

    Args:
        sheet: The dated sheet.
        model_id: The model to price.
        currency: The currency the caller asked for.

    Returns:
        That model's per-million prices.

    Raises:
        PriceSheetError: When the sheet's currency is not the requested one,
            it carries no metered block, or it prices no such model. The
            requested model id is not echoed back.
    """
    if sheet.currency != currency:
        raise PriceSheetError(_WRONG_CURRENCY)
    metered = sheet.metered_per_million
    if metered is None:
        raise PriceSheetError(_NO_METERED_BLOCK)
    prices = metered.get(model_id)
    if prices is None:
        raise PriceSheetError(_NO_METERED_MODEL)
    return prices


def metered_estimate(
    sheet: PriceSheet,
    *,
    model_id: str,
    currency: Currency,
    tokens: TokensPerReflection,
    monthly_reflection_cap: int = DEFAULT_MONTHLY_REFLECTION_CAP,
    volumes: Sequence[int] = DEFAULT_METERED_VOLUMES,
    input_tokens_spent: int = 0,
    output_tokens_spent: int = 0,
) -> MeteredEstimate:
    """Price the metered pool from a dated sheet and a *measured* token block.

    One reflection costs ``prompt x input_price / 1e6 + output x output_price
    / 1e6``, at the worst measured counts across the surfaces #1909 measured.
    The worst surface is deliberate: the monthly allowance is shared across
    reflections and letters, so any one charged generation could be the
    expensive one. INTIMATE content never reaches the pool (Decision 5) and so
    contributes nothing here — the allowance priced is the pooled one, not the
    account's whole journal.

    Metering has no fixed component, so cost per 1,000 reflections is the same
    at every volume. That equality is R6's finding about O5, not a defect: it
    is what makes the cap, not the volume, the brake.

    No network request, no vendor call, no provisioned resource: ADR-0016
    Decision 4 authorizes no spend, and every input here is an operator file
    or a hermetic measurement.

    Args:
        sheet: Dated operator prices, carrying a metered block.
        model_id: The priced model to quote.
        currency: The currency the caller expects; a sheet in another is
            refused rather than converted.
        tokens: The measured per-surface token block (#1909). Only a
            ``provider_reported`` basis may price anything.
        monthly_reflection_cap: The per-account monthly allowance.
        volumes: Monthly fleet volumes to report cost per 1,000 at.
        input_tokens_spent: Input tokens the vendor key has already spent.
        output_tokens_spent: Output tokens it has already spent.

    Returns:
        The metered estimate, with Decision 3.4's vendor-key token caps.

    Raises:
        PriceSheetError: When the sheet cannot price *model_id* in *currency*.
        ValueError: When *tokens* is not provider-reported, or the allowance,
            the spent counts or *volumes* are out of range.
    """
    prices = _metered_prices(sheet, model_id, currency)
    prompt, output = measured_reflection_tokens(tokens)
    input_cap, output_cap = pool_cap_tokens(
        tokens=tokens,
        monthly_reflection_cap=monthly_reflection_cap,
        input_tokens_spent=input_tokens_spent,
        output_tokens_spent=output_tokens_spent,
    )
    per_reflection = (
        prompt * prices.input_per_million + output * prices.output_per_million
    ) / _PER_MILLION
    return MeteredEstimate(
        observed_on=sheet.observed_on,
        source=sheet.source,
        currency=currency,
        model_id=model_id,
        token_basis=tokens.basis,
        prompt_tokens_per_reflection=prompt,
        output_tokens_per_reflection=output,
        per_volume=tuple(
            MeteredVolume(
                reflections_per_month=volume,
                tokens_per_month=(prompt + output) * volume,
                cost_per_1000_reflections=_micro_up(per_reflection * _PER_THOUSAND),
                cost_per_month=_micro_up(per_reflection * volume),
            )
            for volume in volumes
        ),
        monthly_reflection_cap=monthly_reflection_cap,
        cost_per_account_month=_micro_up(per_reflection * monthly_reflection_cap),
        vendor_key_input_token_cap=input_cap,
        vendor_key_output_token_cap=output_cap,
    )
