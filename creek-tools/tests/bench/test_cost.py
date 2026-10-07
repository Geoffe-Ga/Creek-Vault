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
    Allocation,
    AllowanceUse,
    DutyBand,
    PriceSheetError,
    cost_bands,
    load_price_sheet,
)
from creek_mcp.provisioning.fly import FlyProviderPolicy

_TODAY = date(2026, 10, 7)
_COST_MODULE = Path(__file__).resolve().parents[2] / "creek_mcp" / "bench" / "cost.py"


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
    with pytest.raises(PriceSheetError, match="invalid"):
        load_price_sheet(path, today=_TODAY)


def test_non_usd_or_unknown_field_refused(tmp_path: Path) -> None:
    """Only USD sheets with exactly the documented fields are read."""
    with pytest.raises(PriceSheetError, match="currency"):
        load_price_sheet(_sheet_file(tmp_path, currency="EUR"), today=_TODAY)
    with pytest.raises(PriceSheetError, match="surprise"):
        load_price_sheet(_sheet_file(tmp_path, surprise="1"), today=_TODAY)


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
