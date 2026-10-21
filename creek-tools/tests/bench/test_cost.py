"""The per-account cost model: exact cents from an operator price sheet.

The figures are a *model*, never a measurement, and the model refuses to run
on prices it cannot date: a sheet with no ``observed_on``, one older than
:data:`MAX_PRICE_AGE_DAYS`, or one from the future. The allocation defaults are
read from the Fly provisioning policy itself, so the model cannot silently
drift from what ``fly.py`` actually provisions.

Every expected value below is recomputed by hand in its docstring.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from creek_mcp.bench.cost import (
    MAX_PRICE_AGE_DAYS,
    PILOT_REFLECTIONS_PER_MONTH,
    PRODUCTION_REFLECTIONS_PER_MONTH,
    Allocation,
    AllowanceUse,
    DutyBand,
    PriceSheetError,
    cost_bands,
    load_price_sheet,
    metered_estimate,
)
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.tokens import (
    Surface,
    SurfaceTokens,
    TokensPerReflection,
    unmeasured_tokens,
)
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.provisioning.fly import FlyProviderPolicy
from creek_mcp.provisioning.pool_bundle import pool_cap_tokens

_TODAY = date(2026, 10, 7)
_COST_MODULE = Path(__file__).resolve().parents[2] / "creek_mcp" / "bench" / "cost.py"

_MODEL_ID = "synthetic-fixture-model"
"""Obviously not a vendor id: no ADR-0016 Options-table figure is a fact."""

_METERED_BLOCK = {
    _MODEL_ID: {"input_per_million": "1.00", "output_per_million": "4.00"}
}
"""Round synthetic prices, chosen so every expectation below divides exactly.

Not a vendor's prices and not ADR-0016's: every figure in that record's
Options table was read through a web-search summary and must be re-read from a
dated sheet before it prices anything.
"""


def _sheet_file(tmp_path: Path, **overrides: Any) -> Path:
    """Write a valid price sheet observed today, overridden by *overrides*."""
    sheet: dict[str, Any] = {
        "observed_on": _TODAY.isoformat(),
        "currency": "USD",
        "source": "fly-pricing-page",
        "machine_monthly_usd": {"shared-1x-1024mb": "5.70"},
        "volume_gb_month_usd": "0.15",
        "rootfs_gb_month_usd": "0.15",
    }
    sheet.update(overrides)
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    return path


def _band(low: str, high: str) -> DutyBand:
    """A duty band from decimal strings."""
    return DutyBand(low=Decimal(low), high=Decimal(high))


def test_usd_per_account_month_exact_fly_defaults(tmp_path: Path) -> None:
    """Fly defaults at 5% and 100% duty: 1.18 and 6.45 USD.

    low  = 5.70*0.05 + 5*0.15 + (1-0.05)*1*0.15 = 0.285 + 0.75 + 0.1425
         = 1.1775 -> 1.18
    high = 5.70*1.00 + 5*0.15 + 0*1*0.15       = 5.70 + 0.75 + 0
         = 6.45
    """
    sheet = load_price_sheet(_sheet_file(tmp_path), today=_TODAY)
    estimate = cost_bands(sheet, Allocation.from_fly_defaults(), _band("0.05", "1"))
    assert estimate.low_usd == Decimal("1.18")
    assert estimate.high_usd == Decimal("6.45")
    assert estimate.kind == "model"
    assert estimate.allocation_key == "shared-1x-1024mb"
    assert estimate.observed_on == _TODAY


def test_usd_exact_custom(tmp_path: Path) -> None:
    """A 50 USD machine at 10%/50% duty: 5.89 and 25.83 USD.

    low  = 50*0.10 + 5*0.15 + 0.90*1*0.15 = 5.00 + 0.75 + 0.135 = 5.885 -> 5.89
    high = 50*0.50 + 5*0.15 + 0.50*1*0.15 = 25.00 + 0.75 + 0.075 = 25.825 -> 25.83
    Both round half-up; half-even would give 5.88 and 25.82.
    """
    path = _sheet_file(tmp_path, machine_monthly_usd={"performance-2x-4096mb": "50.00"})
    allocation = Allocation.from_fly_defaults(
        {"cpu_kind": "performance", "cpus": 2, "memory_mb": 4096}
    )
    estimate = cost_bands(
        load_price_sheet(path, today=_TODAY), allocation, _band("0.10", "0.50")
    )
    assert estimate.low_usd == Decimal("5.89")
    assert estimate.high_usd == Decimal("25.83")


def test_rounding_half_up_to_cents(tmp_path: Path) -> None:
    """An exact half cent rounds up: 0.005 -> 0.01, never 0.00.

    machine 0.01 at duty 0.5, no volume, no rootfs = 0.005 -> 0.01
    """
    path = _sheet_file(
        tmp_path,
        machine_monthly_usd={"shared-1x-1024mb": "0.01"},
        volume_gb_month_usd="0",
        rootfs_gb_month_usd="0",
    )
    estimate = cost_bands(
        load_price_sheet(path, today=_TODAY),
        Allocation.from_fly_defaults(),
        _band("0.5", "0.5"),
    )
    assert estimate.low_usd == Decimal("0.01")


def test_allowance_duty_and_cost(tmp_path: Path) -> None:
    """20 reflections of 10 s plus 290 s linger: duty 6000/2628000.

    730 billable hours = 2,628,000 s. duty = 20*(10+290)/2628000
    = 0.002283105...; cost = 5.70*duty + 0.75 + (1-duty)*0.15
    = 0.0130137 + 0.75 + 0.1496575 = 0.9126712 -> 0.91
    """
    use = AllowanceUse(
        reflections_per_month=20,
        seconds_per_reflection=Decimal(10),
        linger_seconds=Decimal(290),
    )
    assert use.duty() == Decimal(6000) / Decimal(2_628_000)
    estimate = cost_bands(
        load_price_sheet(_sheet_file(tmp_path), today=_TODAY),
        Allocation.from_fly_defaults(),
        _band("0", "1"),
        allowance=use,
    )
    assert estimate.allowance_usd == Decimal("0.91")
    assert estimate.allowance_duty == Decimal("0.002283")


def test_allowance_duty_saturates_at_one() -> None:
    """A machine cannot run more than the whole month."""
    use = AllowanceUse(
        reflections_per_month=10_000,
        seconds_per_reflection=Decimal(10),
        linger_seconds=Decimal(290),
    )
    assert use.duty() == Decimal(1)


def test_no_allowance_leaves_allowance_fields_unset(tmp_path: Path) -> None:
    """Without an allowance, no allowance figure is invented."""
    estimate = cost_bands(
        load_price_sheet(_sheet_file(tmp_path), today=_TODAY),
        Allocation.from_fly_defaults(),
        _band("0", "1"),
    )
    assert estimate.allowance_usd is None
    assert estimate.allowance_duty is None


def test_missing_observed_on_raises(tmp_path: Path) -> None:
    """An undated price sheet is refused."""
    path = _sheet_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["observed_on"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PriceSheetError, match="observed_on"):
        load_price_sheet(path, today=_TODAY)


def test_stale_sheet_refused_at_91_accepted_at_90(tmp_path: Path) -> None:
    """Prices exactly :data:`MAX_PRICE_AGE_DAYS` old are accepted; a day more is not."""
    assert MAX_PRICE_AGE_DAYS == 90
    edge = (_TODAY - timedelta(days=MAX_PRICE_AGE_DAYS)).isoformat()
    sheet = load_price_sheet(_sheet_file(tmp_path, observed_on=edge), today=_TODAY)
    assert sheet.observed_on.isoformat() == edge
    stale = (_TODAY - timedelta(days=MAX_PRICE_AGE_DAYS + 1)).isoformat()
    with pytest.raises(PriceSheetError, match="stale"):
        load_price_sheet(_sheet_file(tmp_path, observed_on=stale), today=_TODAY)


def test_future_observed_on_refused(tmp_path: Path) -> None:
    """Prices observed after today cannot have been observed."""
    tomorrow = (_TODAY + timedelta(days=1)).isoformat()
    with pytest.raises(PriceSheetError, match="future"):
        load_price_sheet(_sheet_file(tmp_path, observed_on=tomorrow), today=_TODAY)


def test_missing_file_refused(tmp_path: Path) -> None:
    """No sheet, no estimate."""
    with pytest.raises(PriceSheetError, match="not found"):
        load_price_sheet(tmp_path / "absent.json", today=_TODAY)


def test_malformed_json_refused(tmp_path: Path) -> None:
    """A sheet that is not JSON is refused, not half-read."""
    path = tmp_path / "prices.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PriceSheetError, match=r"^price sheet is invalid: not JSON$"):
        load_price_sheet(path, today=_TODAY)


def test_admitted_currencies_and_unknown_field_refused(tmp_path: Path) -> None:
    """EUR is admitted (#1897); an unlisted code or stray field is not.

    ADR-0016's O5 candidates are quoted in both EUR and USD, so the sheet must
    be able to say which. The machine-month model still prices USD only, and
    refuses a EUR sheet at use rather than pretending its euros are dollars.
    """
    assert load_price_sheet(_sheet_file(tmp_path, currency="EUR"), today=_TODAY)
    with pytest.raises(PriceSheetError, match="currency"):
        load_price_sheet(_sheet_file(tmp_path, currency="GBP"), today=_TODAY)
    with pytest.raises(PriceSheetError, match="surprise"):
        load_price_sheet(_sheet_file(tmp_path, surprise="1"), today=_TODAY)


def test_machine_month_refuses_a_non_usd_sheet(tmp_path: Path) -> None:
    """``cost_bands`` reports ``*_usd`` fields, so it will not price euros."""
    sheet = load_price_sheet(_sheet_file(tmp_path, currency="EUR"), today=_TODAY)
    with pytest.raises(PriceSheetError, match="currency"):
        cost_bands(sheet, Allocation.from_fly_defaults(), _band("0", "1"))


def test_machine_month_refuses_a_sheet_without_machine_prices(
    tmp_path: Path,
) -> None:
    """A metered-only O5 sheet carries no Fly prices; that is named, not guessed."""
    path = tmp_path / "metered-only.json"
    path.write_text(
        json.dumps(
            {
                "observed_on": _TODAY.isoformat(),
                "currency": "USD",
                "source": "synthetic-fixture",
                "metered_per_million": _METERED_BLOCK,
            }
        ),
        encoding="utf-8",
    )
    sheet = load_price_sheet(path, today=_TODAY)
    with pytest.raises(PriceSheetError, match="machine_monthly_usd"):
        cost_bands(sheet, Allocation.from_fly_defaults(), _band("0", "1"))


def test_unknown_allocation_key_refused(tmp_path: Path) -> None:
    """An allocation the sheet does not price is refused, not guessed."""
    sheet = load_price_sheet(_sheet_file(tmp_path), today=_TODAY)
    allocation = Allocation.from_fly_defaults({"memory_mb": 2048})
    with pytest.raises(PriceSheetError, match="shared-1x-2048mb"):
        cost_bands(sheet, allocation, _band("0", "1"))


@pytest.mark.parametrize(
    ("low", "high"), [("-0.1", "0.5"), ("0.6", "0.5"), ("0", "1.01")]
)
def test_duty_band_validation(low: str, high: str) -> None:
    """Duty is a fraction of the month: ``0 <= low <= high <= 1``."""
    with pytest.raises(ValidationError):
        _band(low, high)


def test_allocation_tracks_fly_policy_defaults() -> None:
    """The default allocation is whatever ``FlyProviderPolicy`` defaults to."""
    defaults = {
        field.name: field.default
        for field in dataclasses.fields(FlyProviderPolicy)
        if field.default is not dataclasses.MISSING
    }
    allocation = Allocation.from_fly_defaults()
    assert allocation.cpu_kind == defaults["cpu_kind"]
    assert allocation.cpus == defaults["cpus"]
    assert allocation.memory_mb == defaults["memory_mb"]
    assert allocation.rootfs_gb == defaults["rootfs_size_gb"]
    assert allocation.volume_gb == defaults["volume_size_gb"]


def test_allocation_follows_a_changed_fly_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Change every policy default; the allocation must move with each.

    Equality with today's defaults cannot tell a read from a restated
    literal, because the two agree; a changed default can.
    """
    fields = {field.name: field for field in dataclasses.fields(FlyProviderPolicy)}
    changed = {
        "cpu_kind": "performance",
        "cpus": 3,
        "memory_mb": 3072,
        "rootfs_size_gb": 4,
        "volume_size_gb": 9,
    }
    for name, value in changed.items():
        monkeypatch.setattr(fields[name], "default", value)
    allocation = Allocation.from_fly_defaults()
    assert allocation.key == "performance-3x-3072mb"
    assert allocation.rootfs_gb == 4
    assert allocation.volume_gb == 9


def test_allocation_override_is_validated() -> None:
    """Overrides go through validation; a non-positive size is refused."""
    with pytest.raises(ValidationError):
        Allocation.from_fly_defaults({"cpus": 0})


def test_cost_module_makes_no_network() -> None:
    """The cost model fetches nothing: prices come only from the operator's file."""
    tree = ast.parse(_COST_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"httpx", "urllib", "socket", "requests", "http"})


# --------------------------------------------------------------------------
# Metered per-token pricing (#1897).
# --------------------------------------------------------------------------


def _metered_sheet_file(tmp_path: Path, **overrides: Any) -> Path:
    """Write a metered sheet observed today, overridden by *overrides*."""
    sheet: dict[str, Any] = {
        "observed_on": _TODAY.isoformat(),
        "currency": "USD",
        "source": "synthetic-fixture",
        "metered_per_million": _METERED_BLOCK,
    }
    sheet.update(overrides)
    path = tmp_path / "metered.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    return path


def _measured(
    *, prompt: int, output: int, basis: TokenBasis = TokenBasis.PROVIDER_REPORTED
) -> TokensPerReflection:
    """Return a token block whose worst surface is *prompt* / *output*."""
    block = unmeasured_tokens()
    return TokensPerReflection(
        basis=basis,
        per_surface={
            Surface.REFLECTION_GROUNDED: SurfaceTokens(
                surface=Surface.REFLECTION_GROUNDED,
                prompt_tokens=prompt,
                output_tokens=output,
                max_output_tokens=128,
                output_cap_exceeded=False,
                outcome=Outcome.OK,
            )
        },
        prompt_overhead_tokens=10,
        prompt_overhead_bound=block.prompt_overhead_bound,
        prompt_overhead_within_bound=True,
        assumed_prompt_tokens=block.assumed_prompt_tokens,
        assumed_output_tokens=block.assumed_output_tokens,
    )


def test_metered_cost_per_1000_and_per_account_month_exact(tmp_path: Path) -> None:
    """1,000 prompt + 100 output tokens at 1.00 / 4.00 per million.

    per reflection = 1000*1.00/1e6 + 100*4.00/1e6 = 0.001 + 0.0004 = 0.0014
    per 1,000      = 0.0014 * 1000               = 1.400000
    per account-mo = 0.0014 * 20                 = 0.028000
    pilot month    = 0.0014 * 100                = 0.140000
    10k month      = 0.0014 * 10000              = 14.000000
    """
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    estimate = metered_estimate(
        sheet,
        model_id=_MODEL_ID,
        currency="USD",
        tokens=_measured(prompt=1_000, output=100),
    )
    assert estimate.kind == "model"
    assert estimate.shape == "metered_tokens"
    assert estimate.token_basis is TokenBasis.PROVIDER_REPORTED
    assert estimate.prompt_tokens_per_reflection == 1_000
    assert estimate.output_tokens_per_reflection == 100
    assert estimate.cost_per_account_month == Decimal("0.028000")
    assert estimate.monthly_reflection_cap == 20
    volumes = {one.reflections_per_month: one for one in estimate.per_volume}
    assert set(volumes) == {
        PILOT_REFLECTIONS_PER_MONTH,
        PRODUCTION_REFLECTIONS_PER_MONTH,
    }
    pilot = volumes[PILOT_REFLECTIONS_PER_MONTH]
    production = volumes[PRODUCTION_REFLECTIONS_PER_MONTH]
    assert pilot.cost_per_1000_reflections == Decimal("1.400000")
    assert pilot.cost_per_month == Decimal("0.140000")
    assert pilot.tokens_per_month == 110_000
    assert production.cost_per_month == Decimal("14.000000")
    assert production.tokens_per_month == 11_000_000
    # Metering has no fixed component, so volume buys no discount. That
    # equality is R6's finding, not a bug.
    assert production.cost_per_1000_reflections == pilot.cost_per_1000_reflections


def test_metered_vendor_key_cap_matches_decision_3_item_4(tmp_path: Path) -> None:
    """spent + 20 x measured tokens x 2, per direction; mutating 2 breaks it."""
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    estimate = metered_estimate(
        sheet,
        model_id=_MODEL_ID,
        currency="USD",
        tokens=_measured(prompt=1_000, output=100),
        input_tokens_spent=5_000,
        output_tokens_spent=500,
    )
    assert estimate.vendor_key_input_token_cap == 5_000 + 20 * 1_000 * 2
    assert estimate.vendor_key_output_token_cap == 500 + 20 * 100 * 2


def test_metered_cap_is_the_one_in_pool_bundle(tmp_path: Path) -> None:
    """The cap is not re-derived here: #1914's sizing function supplies it."""
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    tokens = _measured(prompt=777, output=55)
    estimate = metered_estimate(
        sheet, model_id=_MODEL_ID, currency="USD", tokens=tokens
    )
    expected_input, expected_output = pool_cap_tokens(
        tokens=tokens,
        monthly_reflection_cap=estimate.monthly_reflection_cap,
        input_tokens_spent=0,
        output_tokens_spent=0,
    )
    assert estimate.vendor_key_input_token_cap == expected_input
    assert estimate.vendor_key_output_token_cap == expected_output


@pytest.mark.parametrize(
    "basis",
    [TokenBasis.BYTE_UPPER_BOUND, TokenBasis.UNREPORTED, TokenBasis.NOT_MEASURED],
)
def test_metered_refuses_every_basis_but_provider_reported(
    tmp_path: Path, basis: TokenBasis
) -> None:
    """A bound and two absences are not prices. Refuse rather than estimate."""
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    with pytest.raises(ValueError, match="provider-reported"):
        metered_estimate(
            sheet,
            model_id=_MODEL_ID,
            currency="USD",
            tokens=_measured(prompt=1_000, output=100, basis=basis),
        )


def test_metered_never_falls_back_to_the_adr_assumption(tmp_path: Path) -> None:
    """An unmeasured block is refused, not priced at 2,500 + 128."""
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    with pytest.raises(ValueError, match="provider-reported"):
        metered_estimate(
            sheet, model_id=_MODEL_ID, currency="USD", tokens=unmeasured_tokens()
        )


def test_metered_refuses_a_currency_mismatch(tmp_path: Path) -> None:
    """A EUR sheet asked for in USD is refused, never silently converted."""
    sheet = load_price_sheet(
        _metered_sheet_file(tmp_path, currency="EUR"), today=_TODAY
    )
    with pytest.raises(PriceSheetError, match="currency"):
        metered_estimate(
            sheet,
            model_id=_MODEL_ID,
            currency="USD",
            tokens=_measured(prompt=1_000, output=100),
        )
    priced = metered_estimate(
        sheet,
        model_id=_MODEL_ID,
        currency="EUR",
        tokens=_measured(prompt=1_000, output=100),
    )
    assert priced.currency == "EUR"


def test_metered_refuses_a_sheet_with_no_metered_block(tmp_path: Path) -> None:
    """A machine-month sheet cannot price tokens; the missing field is named."""
    sheet = load_price_sheet(_sheet_file(tmp_path), today=_TODAY)
    with pytest.raises(PriceSheetError, match="metered_per_million"):
        metered_estimate(
            sheet,
            model_id=_MODEL_ID,
            currency="USD",
            tokens=_measured(prompt=1_000, output=100),
        )


def test_metered_refuses_an_unpriced_model(tmp_path: Path) -> None:
    """A model the sheet does not price is refused, and its id is not echoed."""
    sheet = load_price_sheet(_metered_sheet_file(tmp_path), today=_TODAY)
    with pytest.raises(PriceSheetError) as excinfo:
        metered_estimate(
            sheet,
            model_id="another-synthetic-model",
            currency="USD",
            tokens=_measured(prompt=1_000, output=100),
        )
    assert "another-synthetic-model" not in str(excinfo.value)


def test_metered_rounds_up_never_down(tmp_path: Path) -> None:
    """A sub-micro-dollar figure rounds up: rounding down understates a bill."""
    sheet = load_price_sheet(
        _metered_sheet_file(
            tmp_path,
            metered_per_million={
                _MODEL_ID: {
                    "input_per_million": "0.0000001",
                    "output_per_million": "0",
                }
            },
        ),
        today=_TODAY,
    )
    estimate = metered_estimate(
        sheet, model_id=_MODEL_ID, currency="USD", tokens=_measured(prompt=1, output=1)
    )
    assert estimate.cost_per_account_month == Decimal("0.000001")


def test_metered_stale_and_future_sheets_are_refused(tmp_path: Path) -> None:
    """The dated-sheet rules are the load's, verbatim, metered or not."""
    stale = _metered_sheet_file(
        tmp_path, observed_on=(_TODAY - timedelta(days=91)).isoformat()
    )
    with pytest.raises(PriceSheetError, match="stale"):
        load_price_sheet(stale, today=_TODAY)
    future = _metered_sheet_file(
        tmp_path, observed_on=(_TODAY + timedelta(days=1)).isoformat()
    )
    with pytest.raises(PriceSheetError, match="future"):
        load_price_sheet(future, today=_TODAY)
    undated = tmp_path / "undated.json"
    undated.write_text(
        json.dumps(
            {
                "currency": "USD",
                "source": "synthetic-fixture",
                "metered_per_million": _METERED_BLOCK,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PriceSheetError, match="observed_on"):
        load_price_sheet(undated, today=_TODAY)


def test_no_adr_options_table_price_is_hard_coded() -> None:
    """AC: no ADR-0016 Options-table figure appears as a current price."""
    source = _COST_MODULE.read_text(encoding="utf-8")
    for figure in ("0.43", "1.70", "0.15 ", "0.60", "0.40", "1.25", "1.29", "0.45"):
        assert figure not in source, figure
