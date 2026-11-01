"""Local pool spend metering and the 50%-of-cap alert (#2007, R4-1, R4-2).

The owner's 2026-11-01 decision on #2007: *meter locally* — count the tokens
Creek sends through the pool, price them at the vendor's documented rate, and
alert at EUR 5 (half the EUR 10 monthly key cap). No vendor usage API is
polled. Prices live in a pinned table, never in code, and the table ships with
Privatemode marked operator-must-fill, so until the operator pins prices the
meter reports tokens only and says ``price-unpinned``.

Every figure here is synthetic. No vendor is contacted, nothing is billed, and
the only prices in this module are invented test values on an invented vendor.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest

from creek.classify.llm import pool_meter
from creek.classify.llm.pool_meter import (
    ESTIMATE_BYTES_PER_TOKEN,
    LEDGER_RELPATH,
    SPEND_ALERT_KIND,
    UNANSWERED_COMPLETION_ESTIMATE,
    PoolCallKind,
    PoolSpendMeter,
    PoolUsage,
    SpendState,
    active_pool_meter,
    meter_answer,
    meter_unanswered,
    pool_spend_meter,
    usage_from_answer,
    usage_unanswered,
)
from creek.classify.llm.pool_prices import (
    DEFAULT_POOL_ALERT_FRACTION,
    DEFAULT_POOL_MONTHLY_CAP,
    PRICE_TABLE_PATH,
    PriceTable,
    PriceTableError,
    load_price_table,
)
from creek_mcp.provisioning.budget import AlertKind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

VENDOR: Final[str] = "examplevendor"
"""An invented vendor: its prices below are test values, not anyone's rates."""

MODEL: Final[str] = "example-chat-1"
EMBED_MODEL: Final[str] = "example-embed-1"
UNPRICED_MODEL: Final[str] = "example-new-chat-2"
"""A model id the invented vendor serves but the table does not price."""
PROMPT_CANARY: Final[str] = "journal-canary-5b1d plaintext entry"
REQUEST_ID_CANARY: Final[str] = "req-canary-0c9e"
NOVEMBER: Final[datetime] = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
DECEMBER: Final[datetime] = datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC)
MILLION: Final[int] = 1_000_000


def _pinned_entry(**overrides: object) -> dict[str, object]:
    """Return a pinned vendor entry with invented prices."""
    entry: dict[str, object] = {
        "status": "pinned",
        "currency": "EUR",
        "source_url": "https://prices.example.test/pricing",
        "read_on": "2026-10-30",
        "monthly_cap": "10.00",
        "alert_fraction": "0.5",
        "models": {
            MODEL: {
                "prompt_per_million": "1.00",
                "completion_per_million": "2.00",
            },
            EMBED_MODEL: {"embedding_per_million": "0.50"},
        },
    }
    entry.update(overrides)
    return entry


def _table(**vendors: Mapping[str, object]) -> PriceTable:
    """Return a validated price table carrying *vendors*."""
    return PriceTable.model_validate({"schema_version": 1, "vendors": vendors})


def _pinned() -> PriceTable:
    """A table pinning the invented vendor."""
    return _table(**{VENDOR: _pinned_entry()})


def _unpinned() -> PriceTable:
    """A table that, like the shipped one, leaves the vendor operator-must-fill."""
    return _table(
        **{
            VENDOR: {
                "status": "operator_must_fill",
                "currency": "EUR",
                "source_url": None,
                "read_on": None,
                "models": {},
            }
        }
    )


class Clock:
    """A settable UTC clock."""

    def __init__(self, now: datetime = NOVEMBER) -> None:
        """Start at *now*."""
        self.now = now

    def __call__(self) -> datetime:
        """Return the current reading."""
        return self.now


def _meter(
    vault: Path, prices: PriceTable | None = None, clock: Clock | None = None
) -> PoolSpendMeter:
    """Return a meter over *vault* with the invented pinned table by default."""
    return PoolSpendMeter(
        vault, prices=_pinned() if prices is None else prices, clock=clock or Clock()
    )


def _prompt(tokens: int) -> PoolUsage:
    """Usage of *tokens* reported prompt tokens and nothing else."""
    return PoolUsage(prompt_tokens=tokens, completion_tokens=0, embedding_tokens=0)


def _alerts(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the decoded spend-alert records written to ``creek.pool``."""
    records = []
    for record in caplog.records:
        if record.name != pool_meter.TELEMETRY_LOGGER:
            continue
        decoded = json.loads(record.getMessage())
        if decoded.get("alert") == SPEND_ALERT_KIND:
            records.append(decoded)
    return records


# --------------------------------------------------------------------------
# Usage extraction: provider-reported first, a conservative estimate otherwise.
# --------------------------------------------------------------------------


def test_reported_generation_usage_is_taken_as_reported() -> None:
    """An OpenAI-compatible ``usage`` block is the measured count."""
    answer = {"usage": {"prompt_tokens": 120, "completion_tokens": 33}}

    usage = usage_from_answer(PoolCallKind.GENERATION, {"model": MODEL}, answer)

    assert usage == PoolUsage(
        prompt_tokens=120, completion_tokens=33, embedding_tokens=0, estimated=False
    )


def test_reported_embedding_usage_counts_as_embedding_tokens() -> None:
    """An embeddings answer reports prompt tokens; they are embedding tokens."""
    answer = {"usage": {"prompt_tokens": 64, "total_tokens": 64}}

    usage = usage_from_answer(PoolCallKind.EMBEDDING, {"input": ["a"]}, answer)

    assert usage == PoolUsage(
        prompt_tokens=0, completion_tokens=0, embedding_tokens=64, estimated=False
    )


def test_an_embedding_answer_with_only_total_tokens_is_still_reported() -> None:
    """Some servers report ``total_tokens`` alone for embeddings."""
    usage = usage_from_answer(
        PoolCallKind.EMBEDDING, {"input": ["a"]}, {"usage": {"total_tokens": 9}}
    )

    assert usage.embedding_tokens == 9
    assert not usage.estimated


def _body(prompt: str = PROMPT_CANARY) -> dict[str, object]:
    """A chat body carrying *prompt*."""
    return {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"choices": []}, id="no-usage"),
        pytest.param({"usage": None}, id="null-usage"),
        pytest.param({"usage": {"prompt_tokens": True}}, id="bool-count"),
        pytest.param({"usage": {"prompt_tokens": -4}}, id="negative-count"),
        pytest.param({"usage": {"prompt_tokens": "12"}}, id="string-count"),
        pytest.param(["not", "an", "answer"], id="not-a-mapping"),
    ],
)
def test_a_missing_usage_block_is_estimated_as_an_upper_bound(answer: object) -> None:
    """Never under-count: no usable report means bytes, flagged as an estimate."""
    prompt = "x" * 500
    body = _body(prompt)

    usage = usage_from_answer(PoolCallKind.GENERATION, body, answer)

    assert usage.estimated
    encoded = json.dumps(body, separators=(",", ":")).encode()
    assert usage.prompt_tokens == len(encoded) // ESTIMATE_BYTES_PER_TOKEN
    assert usage.prompt_tokens >= len(prompt)
    assert usage.completion_tokens >= 1


def test_the_completion_estimate_bounds_the_answer_text() -> None:
    """An answer without usage is costed at least at its own text length."""
    text = "y" * 700
    answer = {"choices": [{"message": {"content": text}}]}

    usage = usage_from_answer(PoolCallKind.GENERATION, _body(), answer)

    assert usage.estimated
    assert usage.completion_tokens >= len(text)


def test_a_partial_report_estimates_only_the_missing_half() -> None:
    """A reported prompt count is kept; the absent completion is estimated."""
    answer = {
        "usage": {"prompt_tokens": 40},
        "choices": [{"message": {"content": "z" * 90}}],
    }

    usage = usage_from_answer(PoolCallKind.GENERATION, _body(), answer)

    assert usage.prompt_tokens == 40
    assert usage.completion_tokens >= 90
    assert usage.estimated


def test_an_embedding_answer_without_usage_is_estimated_from_the_request() -> None:
    """The texts sent bound the tokens an embeddings call can bill."""
    body = {"model": EMBED_MODEL, "input": ["w" * 300, "v" * 200]}

    usage = usage_from_answer(PoolCallKind.EMBEDDING, body, {"data": []})

    assert usage.estimated
    assert usage.embedding_tokens >= 500
    assert usage.completion_tokens == 0


def test_an_unanswered_generation_counts_the_prompt_and_its_ceiling() -> None:
    """A dial that sent its body may still be billed; count it, flagged."""
    body = {**_body("q" * 250), "max_tokens": 128}

    usage = usage_unanswered(PoolCallKind.GENERATION, body)

    assert usage.estimated
    assert usage.prompt_tokens >= 250
    assert usage.completion_tokens == 128


def test_an_unanswered_generation_without_a_ceiling_uses_the_named_estimate() -> None:
    """No ``max_tokens`` means the documented planning figure, flagged."""
    usage = usage_unanswered(PoolCallKind.GENERATION, _body())

    assert usage.completion_tokens == UNANSWERED_COMPLETION_ESTIMATE
    assert usage.estimated


def test_an_unanswered_embedding_counts_only_the_texts() -> None:
    """Embeddings have no completion half."""
    usage = usage_unanswered(PoolCallKind.EMBEDDING, {"input": ["s" * 40]})

    assert usage.embedding_tokens >= 40
    assert usage.completion_tokens == 0
    assert usage.prompt_tokens == 0


# --------------------------------------------------------------------------
# The price table: pinned configuration, never code, and never invented.
# --------------------------------------------------------------------------


def test_the_shipped_table_leaves_privatemode_for_the_operator_to_fill() -> None:
    """No Privatemode price is invented: the entry is operator-must-fill."""
    table = load_price_table()
    entry = table.vendor("privatemode")

    assert entry is not None
    assert not entry.pinned
    assert entry.status == "operator_must_fill"
    assert entry.models == {}
    assert entry.source_url is None
    assert entry.read_on is None
    assert entry.currency == "EUR"


def test_the_shipped_table_carries_the_owner_cap_and_fraction() -> None:
    """R4-1's EUR 10 cap and R4-2's 50% are named config, defaulted the same."""
    entry = load_price_table().vendor("privatemode")

    assert entry is not None
    assert entry.monthly_cap == DEFAULT_POOL_MONTHLY_CAP == Decimal("10.00")
    assert entry.alert_fraction == DEFAULT_POOL_ALERT_FRACTION == Decimal("0.5")
    assert entry.alert_threshold == Decimal("5.000")


def test_the_shipped_table_is_the_packaged_file() -> None:
    """The table is data shipped beside the code, not a code constant."""
    assert PRICE_TABLE_PATH.name == "pool_prices.json"
    assert PRICE_TABLE_PATH.is_file()


def test_an_omitted_cap_and_fraction_take_the_named_defaults() -> None:
    """The defaults apply when the operator writes neither."""
    entry = _pinned_entry()
    del entry["monthly_cap"]
    del entry["alert_fraction"]

    vendor = _table(**{VENDOR: entry}).vendor(VENDOR)

    assert vendor is not None
    assert vendor.monthly_cap == DEFAULT_POOL_MONTHLY_CAP
    assert vendor.alert_fraction == DEFAULT_POOL_ALERT_FRACTION


def test_a_new_vendor_is_a_new_entry_not_code() -> None:
    """Switching vendors is one more table entry, priced the same way."""
    other = "othervendor"
    table = _table(**{VENDOR: _pinned_entry(), other: _pinned_entry()})

    assert table.vendor(other) is not None
    assert table.vendor("absent") is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"source_url": None}, "source_url", id="pinned-without-source"),
        pytest.param({"read_on": None}, "read_on", id="pinned-without-date"),
        pytest.param({"models": {}}, "models", id="pinned-without-models"),
        pytest.param(
            {"source_url": "http://prices.example.test/"}, "source_url", id="not-https"
        ),
        pytest.param({"monthly_cap": "0"}, "monthly_cap", id="zero-cap"),
        pytest.param({"alert_fraction": "0"}, "alert_fraction", id="zero-fraction"),
        pytest.param({"alert_fraction": "1.5"}, "alert_fraction", id="over-one"),
        pytest.param({"currency": "GBP"}, "currency", id="unknown-currency"),
        pytest.param({"surprise": 1}, "surprise", id="unknown-field"),
        pytest.param({"status": "guessed"}, "status", id="unknown-status"),
    ],
)
def test_a_malformed_entry_is_refused_naming_the_field(
    tmp_path: Path, overrides: dict[str, object], field: str
) -> None:
    """Refusals name the offending field, never the operator's values."""
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "vendors": {VENDOR: _pinned_entry(**overrides)}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(PriceTableError) as caught:
        load_price_table(path)

    assert field in str(caught.value)
    assert "prices.example.test" not in str(caught.value)


def test_an_operator_must_fill_entry_may_not_carry_prices(tmp_path: Path) -> None:
    """A half-filled entry is refused rather than read as either state."""
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vendors": {VENDOR: _pinned_entry(status="operator_must_fill")},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PriceTableError, match="models"):
        load_price_table(path)


def test_a_negative_price_is_refused(tmp_path: Path) -> None:
    """A price below zero would subtract spend."""
    entry = _pinned_entry(models={MODEL: {"prompt_per_million": "-1"}})
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps({"schema_version": 1, "vendors": {VENDOR: entry}}), encoding="utf-8"
    )

    with pytest.raises(PriceTableError, match="prompt_per_million"):
        load_price_table(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(None, "not found", id="missing"),
        pytest.param("{not json", "not JSON", id="not-json"),
        pytest.param('{"schema_version": 2, "vendors": {}}', "schema_version", id="v2"),
    ],
)
def test_an_unusable_table_file_is_refused(
    tmp_path: Path, content: str | None, message: str
) -> None:
    """Missing, malformed and unknown-schema tables are each named."""
    path = tmp_path / "prices.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(PriceTableError, match=message):
        load_price_table(path)


def test_json_numbers_are_read_as_exact_decimals(tmp_path: Path) -> None:
    """A price written as a JSON number never passes through binary float."""
    entry = _pinned_entry(models={MODEL: {"prompt_per_million": 0.1}})
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps({"schema_version": 1, "vendors": {VENDOR: entry}}), encoding="utf-8"
    )

    vendor = load_price_table(path).vendor(VENDOR)

    assert vendor is not None
    assert vendor.models[MODEL].prompt_per_million == Decimal("0.1")


def test_a_pinned_date_parses_as_a_date() -> None:
    """``read_on`` is the date the operator read the vendor's page."""
    vendor = _pinned().vendor(VENDOR)

    assert vendor is not None
    assert vendor.read_on == date(2026, 10, 30)


# --------------------------------------------------------------------------
# The meter: a durable, per-vault, calendar-month (UTC) accumulator.
# --------------------------------------------------------------------------


def test_usage_accumulates_per_model_on_the_vault_volume(tmp_path: Path) -> None:
    """Two calls add up, and the ledger lives under the vault itself."""
    meter = _meter(tmp_path)

    meter.record(VENDOR, MODEL, PoolUsage(100, 20, 0))
    spend = meter.record(VENDOR, MODEL, PoolUsage(50, 5, 0))

    assert (spend.prompt_tokens, spend.completion_tokens) == (150, 25)
    assert spend.calls == 2
    assert spend.estimated_calls == 0
    assert spend.month == "2026-11"
    assert (tmp_path / LEDGER_RELPATH).is_file()


def test_the_spend_is_priced_from_the_table(tmp_path: Path) -> None:
    """EUR = tokens x per-million price, per direction and per model."""
    meter = _meter(tmp_path)

    meter.record(VENDOR, MODEL, PoolUsage(MILLION, MILLION, 0))
    spend = meter.record(VENDOR, EMBED_MODEL, PoolUsage(0, 0, 2 * MILLION))

    assert spend.state is SpendState.UNDER_THRESHOLD
    assert spend.spend == Decimal("4.00")
    assert spend.threshold == Decimal("5.00")
    assert spend.currency == "EUR"


def test_spend_is_rounded_up_never_down(tmp_path: Path) -> None:
    """A fraction of a cent reports as the next cent: rounding down understates."""
    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(1))

    assert spend.spend == Decimal("0.01")


def test_estimated_calls_are_counted_and_flagged(tmp_path: Path) -> None:
    """A month carrying estimates says how many of its calls were estimated."""
    meter = _meter(tmp_path)

    meter.record(VENDOR, MODEL, PoolUsage(10, 10, 0, estimated=True))
    spend = meter.record(VENDOR, MODEL, PoolUsage(10, 10, 0))

    assert spend.estimated_calls == 1
    assert "estimated_calls=1" in spend.summary_line()


def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    """A new process on the same volume continues the month, not from zero."""
    _meter(tmp_path).record(VENDOR, MODEL, _prompt(1_000))

    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(500))

    assert spend.prompt_tokens == 1_500
    assert spend.calls == 2


def test_the_month_rolls_over_in_utc(tmp_path: Path) -> None:
    """The first call of a new UTC calendar month starts that month at zero."""
    clock = Clock(NOVEMBER)
    meter = _meter(tmp_path, clock=clock)
    meter.record(VENDOR, MODEL, _prompt(7_000))

    clock.now = DECEMBER
    spend = meter.record(VENDOR, MODEL, _prompt(3))

    assert spend.month == "2026-12"
    assert spend.prompt_tokens == 3
    assert spend.calls == 1


def test_the_month_boundary_is_utc_not_local(tmp_path: Path) -> None:
    """23:30 on 30 November in UTC-8 is already December in UTC."""
    from datetime import timedelta, timezone

    pacific = timezone(timedelta(hours=-8))
    clock = Clock(datetime(2026, 11, 30, 23, 30, tzinfo=pacific))

    spend = _meter(tmp_path, clock=clock).record(VENDOR, MODEL, _prompt(1))

    assert spend.month == "2026-12"


def test_a_report_after_rollover_reads_the_new_month_without_writing(
    tmp_path: Path,
) -> None:
    """Reading last month's ledger in a new month shows no usage yet."""
    clock = Clock(NOVEMBER)
    meter = _meter(tmp_path, clock=clock)
    meter.record(VENDOR, MODEL, _prompt(9))
    ledger = tmp_path / LEDGER_RELPATH
    before = ledger.read_bytes()

    clock.now = DECEMBER
    report = meter.report()

    assert [spend.state for spend in report] == [SpendState.NO_USAGE]
    assert report[0].month == "2026-12"
    assert ledger.read_bytes() == before


def test_a_report_on_a_fresh_vault_creates_nothing(tmp_path: Path) -> None:
    """``--check pool`` on a vault that never dialled writes no ledger."""
    report = _meter(tmp_path).report()

    assert [spend.state for spend in report] == [SpendState.NO_USAGE]
    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_vaults_are_isolated(tmp_path: Path) -> None:
    """One vault's spend is never another's: each volume has its own ledger."""
    first, second = tmp_path / "one", tmp_path / "two"
    _meter(first).record(VENDOR, MODEL, _prompt(4 * MILLION))

    spend = _meter(second).record(VENDOR, MODEL, _prompt(1))

    assert spend.prompt_tokens == 1
    assert spend.spend == Decimal("0.01")


def test_vendors_are_accumulated_and_priced_separately(tmp_path: Path) -> None:
    """A mid-month vendor switch starts the new key's own line."""
    other = "othervendor"
    prices = _table(**{VENDOR: _pinned_entry(), other: _pinned_entry()})
    meter = _meter(tmp_path, prices=prices)
    meter.record(VENDOR, MODEL, _prompt(3 * MILLION))

    spend = meter.record(other, MODEL, _prompt(MILLION))

    assert spend.vendor == other
    assert spend.spend == Decimal("1.00")
    assert {line.vendor for line in meter.report()} == {VENDOR, other}


# --------------------------------------------------------------------------
# The alert: exactly once per month per vault, at >= cap x fraction.
# --------------------------------------------------------------------------


def test_the_alert_kind_is_a_registered_alert_kind() -> None:
    """The kind is already in the closed vocabulary adepthood admits."""
    assert SPEND_ALERT_KIND in {kind.value for kind in AlertKind}
    assert AlertKind.MONTHLY_BUDGET_DEPARTURE.value == SPEND_ALERT_KIND


def test_just_under_half_the_cap_does_not_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """EUR 4.99 of a EUR 10 cap is under the line."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)

    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(4_990_000))

    assert spend.spend == Decimal("4.99")
    assert spend.state is SpendState.UNDER_THRESHOLD
    assert _alerts(caplog) == []


def test_exactly_half_the_cap_alerts_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """EUR 5.00 is the line itself: the comparison is ``>=``."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)

    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(5 * MILLION))

    assert spend.state is SpendState.THRESHOLD_REACHED
    alerts = _alerts(caplog)
    assert len(alerts) == 1
    assert alerts[0]["spend"] == "5.00"
    assert alerts[0]["threshold"] == "5.00"
    assert alerts[0]["currency"] == "EUR"
    assert alerts[0]["component"] == "pool"


def test_crossing_the_line_alerts_once_however_many_calls_follow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Many calls past EUR 5 still produce exactly one alert in the month."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path)

    for _ in range(4):
        meter.record(VENDOR, MODEL, _prompt(2 * MILLION))

    assert len(_alerts(caplog)) == 1


def test_the_once_per_month_latch_survives_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A restarted process does not alert again for the same month."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    _meter(tmp_path).record(VENDOR, MODEL, _prompt(6 * MILLION))

    _meter(tmp_path).record(VENDOR, MODEL, _prompt(MILLION))

    assert len(_alerts(caplog)) == 1


def test_the_alert_re_arms_on_month_rollover(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """December crossing the line alerts again, once."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    clock = Clock(NOVEMBER)
    meter = _meter(tmp_path, clock=clock)
    meter.record(VENDOR, MODEL, _prompt(6 * MILLION))

    clock.now = DECEMBER
    meter.record(VENDOR, MODEL, _prompt(MILLION))
    assert len(_alerts(caplog)) == 1
    meter.record(VENDOR, MODEL, _prompt(5 * MILLION))

    alerts = _alerts(caplog)
    assert [alert["month"] for alert in alerts] == ["2026-11", "2026-12"]


def test_a_custom_cap_and_fraction_move_the_line(tmp_path: Path) -> None:
    """The line is cap x fraction from the table, not a code constant."""
    prices = _table(**{VENDOR: _pinned_entry(monthly_cap="20", alert_fraction="0.25")})

    spend = _meter(tmp_path, prices=prices).record(VENDOR, MODEL, _prompt(5 * MILLION))

    assert spend.threshold == Decimal("5.00")
    assert spend.state is SpendState.THRESHOLD_REACHED


# --------------------------------------------------------------------------
# Unpinned prices: tokens only, an explicit status, and no alert.
# --------------------------------------------------------------------------


def test_unpinned_prices_report_tokens_only_and_never_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No EUR figure is reported or alerted on until the operator pins prices."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)

    spend = _meter(tmp_path, prices=_unpinned()).record(
        VENDOR, MODEL, _prompt(50 * MILLION)
    )

    assert spend.state is SpendState.PRICE_UNPINNED
    assert spend.spend is None
    assert spend.prompt_tokens == 50 * MILLION
    assert _alerts(caplog) == []
    line = spend.summary_line()
    assert "state=price-unpinned" in line
    assert "spend=" not in line


def test_a_vendor_absent_from_the_table_is_unpinned(tmp_path: Path) -> None:
    """The Creek-run pool, or any unlisted vendor, has no EUR figure."""
    spend = _meter(tmp_path).record("creek", MODEL, _prompt(10))

    assert spend.state is SpendState.PRICE_UNPINNED


def test_a_pinned_vendor_whose_only_counted_model_is_unpriced_is_unpinned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With nothing priced there is no lower bound to report: tokens only."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)

    spend = _meter(tmp_path).record(VENDOR, UNPRICED_MODEL, _prompt(50 * MILLION))

    assert spend.state is SpendState.PRICE_UNPINNED
    assert spend.spend is None
    assert spend.spend_lower_bound is None
    assert _alerts(caplog) == []


def test_a_direction_without_a_price_is_unpinned(tmp_path: Path) -> None:
    """Completion tokens on a model priced for prompts only are unpriced."""
    prices = _table(
        **{VENDOR: _pinned_entry(models={MODEL: {"prompt_per_million": "1"}})}
    )

    spend = _meter(tmp_path, prices=prices).record(VENDOR, MODEL, PoolUsage(1, 1, 0))

    assert spend.state is SpendState.PRICE_UNPINNED


def test_unpinned_prices_are_announced_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A content-free ``price_unpinned`` status is raised, not on every call."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path, prices=_unpinned())

    for _ in range(3):
        meter.record(VENDOR, MODEL, _prompt(1))

    statuses = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == pool_meter.TELEMETRY_LOGGER
    ]
    assert [status.get("status") for status in statuses] == ["price_unpinned"]


def test_pinning_prices_later_prices_the_whole_month(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tokens counted while unpinned are priced, and alerted, once pinned."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    _meter(tmp_path, prices=_unpinned()).record(VENDOR, MODEL, _prompt(6 * MILLION))

    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(1))

    assert spend.state is SpendState.THRESHOLD_REACHED
    assert len(_alerts(caplog)) == 1


def test_an_invalid_shipped_table_is_its_own_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table that fails to load is named, never read as zero prices."""
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    monkeypatch.setattr(pool_meter, "PRICE_TABLE_PATH", broken)

    spend = PoolSpendMeter(tmp_path / "vault", clock=Clock()).record(
        VENDOR, MODEL, _prompt(1)
    )

    assert spend.state is SpendState.PRICE_TABLE_INVALID
    assert spend.spend is None


def test_the_default_meter_loads_the_shipped_table(tmp_path: Path) -> None:
    """Out of the box, Privatemode spend is tokens-only and price-unpinned."""
    spend = PoolSpendMeter(tmp_path, clock=Clock()).record(
        "privatemode", "gpt-oss-120b", _prompt(1)
    )

    assert spend.state is SpendState.PRICE_UNPINNED


# --------------------------------------------------------------------------
# Partly priced (#2052): priced models give a lower bound, and it still alerts.
# --------------------------------------------------------------------------


_FULL_ALERT_KEYS: Final = frozenset(
    {
        "event",
        "alert",
        "component",
        "vendor",
        "month",
        "spend",
        "threshold",
        "cap",
        "currency",
    }
)
"""The fully-priced alert record's keys, unchanged by #2052."""


def _statuses(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the decoded status (non-alert) records written to ``creek.pool``."""
    return [
        decoded
        for record in caplog.records
        if record.name == pool_meter.TELEMETRY_LOGGER
        and "status" in (decoded := json.loads(record.getMessage()))
    ]


def test_a_vendor_cost_separates_priced_from_unpriced_models() -> None:
    """The table prices what it can and names the models it cannot."""
    vendor = _pinned().vendor(VENDOR)
    assert vendor is not None

    cost = vendor.price(
        {
            MODEL: (MILLION, 0, 0),
            UNPRICED_MODEL: (MILLION, 0, 0),
            "another-unlisted": (1, 0, 0),
        }
    )

    assert cost is not None
    assert cost.priced == Decimal(1)
    assert cost.priced_models == 1
    assert cost.unpriced == ("another-unlisted", UNPRICED_MODEL)


def test_an_unpinned_vendor_prices_nothing() -> None:
    """An operator-must-fill entry yields no cost at all, not a zero."""
    vendor = _unpinned().vendor(VENDOR)
    assert vendor is not None

    assert vendor.price({MODEL: (1, 0, 0)}) is None


def test_a_priced_model_over_the_line_and_an_unpriced_model_alert_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The lower bound alone reaching EUR 5 alerts, exactly once in the month."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path)
    meter.record(VENDOR, UNPRICED_MODEL, _prompt(MILLION))

    for _ in range(3):
        spend = meter.record(VENDOR, MODEL, _prompt(2 * MILLION))
    _meter(tmp_path).record(VENDOR, MODEL, _prompt(MILLION))

    assert spend.state is SpendState.PRICE_PARTIAL
    assert spend.spend is None
    assert spend.spend_lower_bound == Decimal("6.00")
    assert spend.threshold_reached
    assert spend.unpriced_models == (UNPRICED_MODEL,)
    (alert,) = _alerts(caplog)
    assert alert["price_state"] == "price-partial"
    assert alert["spend_lower_bound"] == "6.00"
    assert "spend" not in alert
    assert alert["unpriced_models"] == 1
    assert alert["unpriced_model_ids"] == [UNPRICED_MODEL]
    assert alert["threshold"] == "5.00"
    assert alert["component"] == "pool"


def test_a_priced_model_under_the_line_and_an_unpriced_model_do_not_alert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Below the line the month is partial and silent, whatever is unpriced."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path)
    meter.record(VENDOR, MODEL, _prompt(4_990_000))

    spend = meter.record(VENDOR, UNPRICED_MODEL, _prompt(50 * MILLION))

    assert spend.state is SpendState.PRICE_PARTIAL
    assert spend.spend is None
    assert spend.spend_lower_bound == Decimal("4.99")
    assert not spend.threshold_reached
    assert _alerts(caplog) == []


def test_the_lower_bound_is_compared_exactly_not_after_rounding(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """EUR 4.9999 reads as 5.00 rounded up, but has not reached the line."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path)
    meter.record(VENDOR, UNPRICED_MODEL, _prompt(1))

    spend = meter.record(VENDOR, MODEL, _prompt(4_999_900))

    assert spend.spend_lower_bound == Decimal("5.00")
    assert not spend.threshold_reached
    assert _alerts(caplog) == []


def test_a_direction_without_a_price_makes_only_that_model_unpriced(
    tmp_path: Path,
) -> None:
    """A model priced for prompts but counted for completions joins the list."""
    prices = _table(
        **{
            VENDOR: _pinned_entry(
                models={
                    MODEL: {"prompt_per_million": "1"},
                    EMBED_MODEL: {"embedding_per_million": "1"},
                }
            )
        }
    )
    meter = _meter(tmp_path, prices=prices)
    meter.record(VENDOR, EMBED_MODEL, PoolUsage(0, 0, 2 * MILLION))

    spend = meter.record(VENDOR, MODEL, PoolUsage(1, 1, 0))

    assert spend.state is SpendState.PRICE_PARTIAL
    assert spend.spend_lower_bound == Decimal("2.00")
    assert spend.unpriced_models == (MODEL,)


def test_a_partial_line_names_its_unpriced_models_in_the_summary(
    tmp_path: Path,
) -> None:
    """``--check pool`` shows a lower bound, the count and the model ids."""
    meter = _meter(tmp_path)
    meter.record(VENDOR, MODEL, PoolUsage(6 * MILLION, 0, 0))
    meter.record(VENDOR, UNPRICED_MODEL, PoolUsage(10, 20, 0))
    meter.record(VENDOR, "example-new-chat-3", PoolUsage(1, 0, 0))

    (line,) = (spend.summary_line() for spend in meter.report())

    assert line == (
        "pool-spend vendor=examplevendor month=2026-11 state=price-partial "
        "prompt_tokens=6000011 completion_tokens=20 embedding_tokens=0 "
        "calls=3 estimated_calls=0 spend_lower_bound=6.00 threshold=5.00 "
        "cap=10.00 currency=EUR threshold_reached=yes unpriced_models=2 "
        "unpriced_model_ids=example-new-chat-2,example-new-chat-3"
    )
    assert " spend=" not in line


def test_a_partial_line_is_announced_once_per_set_of_unpriced_models(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator hears which models to pin, again only when the set grows."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    meter = _meter(tmp_path)
    meter.record(VENDOR, MODEL, _prompt(1))

    for _ in range(3):
        meter.record(VENDOR, UNPRICED_MODEL, _prompt(1))
    meter.record(VENDOR, "example-new-chat-3", _prompt(1))

    assert _statuses(caplog) == [
        {
            "event": "pool_spend",
            "status": "price_partial",
            "vendor": VENDOR,
            "unpriced_models": 1,
            "unpriced_model_ids": [UNPRICED_MODEL],
        },
        {
            "event": "pool_spend",
            "status": "price_partial",
            "vendor": VENDOR,
            "unpriced_models": 2,
            "unpriced_model_ids": [UNPRICED_MODEL, "example-new-chat-3"],
        },
    ]


def test_pinning_the_missing_model_later_does_not_alert_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The month's latch holds across partial and full pricing."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    _meter(tmp_path).record(VENDOR, MODEL, _prompt(6 * MILLION))
    _meter(tmp_path).record(VENDOR, UNPRICED_MODEL, _prompt(1))
    models = {
        MODEL: {"prompt_per_million": "1"},
        UNPRICED_MODEL: {"prompt_per_million": "1"},
    }
    full = _table(**{VENDOR: _pinned_entry(models=models)})

    spend = _meter(tmp_path, prices=full).record(VENDOR, MODEL, _prompt(1))

    assert spend.state is SpendState.THRESHOLD_REACHED
    assert spend.unpriced_models == ()
    assert len(_alerts(caplog)) == 1


def test_a_fully_priced_alert_record_is_unchanged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With every counted model priced, the alert carries exactly its old keys."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)

    spend = _meter(tmp_path).record(VENDOR, MODEL, _prompt(5 * MILLION))

    (alert,) = _alerts(caplog)
    assert set(alert) == _FULL_ALERT_KEYS
    assert spend.spend_lower_bound is None
    assert spend.unpriced_models == ()
    assert _statuses(caplog) == []


def test_a_partial_alert_and_status_carry_no_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A free-text model id is stored and named only as the closed fallback."""
    caplog.set_level(logging.DEBUG)
    meter = _meter(tmp_path)
    meter.record(VENDOR, PROMPT_CANARY, _prompt(1))

    meter.record(VENDOR, MODEL, _prompt(6 * MILLION))

    assert PROMPT_CANARY not in caplog.text
    (alert,) = _alerts(caplog)
    assert alert["unpriced_model_ids"] == [pool_meter.UNRECOGNISED_MODEL]


# --------------------------------------------------------------------------
# A damaged ledger is never silently reset to zero.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('{"schema_version": 9}', id="unknown-schema"),
        pytest.param('["a list"]', id="not-a-mapping"),
        pytest.param(
            '{"schema_version": 1, "month": "2026-11", "vendors": '
            '{"examplevendor": {"alerted": false, "models": '
            '{"example-chat-1": {"prompt_tokens": -1}}}}}',
            id="negative-count",
        ),
    ],
)
def test_an_unreadable_ledger_is_reported_and_never_overwritten(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    """Restarting at zero would under-count; the meter refuses instead."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    ledger = tmp_path / LEDGER_RELPATH
    ledger.parent.mkdir(parents=True)
    ledger.write_text(content, encoding="utf-8")
    meter = _meter(tmp_path)

    spend = meter.record(VENDOR, MODEL, _prompt(1))

    assert spend.state is SpendState.LEDGER_UNREADABLE
    assert ledger.read_text(encoding="utf-8") == content
    assert [line.state for line in meter.report()] == [SpendState.LEDGER_UNREADABLE]
    assert "status=ledger_unreadable" not in caplog.text
    assert '"status": "ledger_unreadable"' in caplog.text


# --------------------------------------------------------------------------
# Content-freeness: counts and money only.
# --------------------------------------------------------------------------


_LEDGER_KEYS: Final = frozenset(
    {
        "schema_version",
        "month",
        "vendors",
        "alerted",
        "models",
        "prompt_tokens",
        "completion_tokens",
        "embedding_tokens",
        "calls",
        "estimated_calls",
    }
)


def _keys(value: object) -> set[str]:
    """Every mapping key in *value*, recursively, excluding vendor/model ids."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in {VENDOR, MODEL, EMBED_MODEL}:
                found.add(key)
            found |= _keys(child)
    return found


def test_the_ledger_and_logs_carry_no_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No prompt, answer or request id reaches the ledger or a log record."""
    caplog.set_level(logging.DEBUG)
    with pool_spend_meter(tmp_path, prices=_pinned(), clock=Clock()):
        body = {**_body(PROMPT_CANARY), "request_id": REQUEST_ID_CANARY}
        meter_answer(
            PoolCallKind.GENERATION,
            VENDOR,
            body,
            status=200,
            payload={
                "choices": [{"message": {"content": PROMPT_CANARY}}],
                "id": REQUEST_ID_CANARY,
            },
        )
        meter_answer(
            PoolCallKind.GENERATION,
            VENDOR,
            body,
            status=200,
            payload={"usage": {"prompt_tokens": 6 * MILLION, "completion_tokens": 1}},
        )

    ledger = (tmp_path / LEDGER_RELPATH).read_text(encoding="utf-8")
    for canary in (PROMPT_CANARY, REQUEST_ID_CANARY):
        assert canary not in ledger
        assert canary not in caplog.text
    assert _keys(json.loads(ledger)) <= _LEDGER_KEYS
    assert len(_alerts(caplog)) == 1


def test_a_model_name_that_is_not_an_identifier_is_not_stored(
    tmp_path: Path,
) -> None:
    """A free-text ``model`` cannot smuggle content into the ledger."""
    meter = _meter(tmp_path)

    meter.record(VENDOR, PROMPT_CANARY, _prompt(1))

    ledger = (tmp_path / LEDGER_RELPATH).read_text(encoding="utf-8")
    assert PROMPT_CANARY not in ledger
    assert pool_meter.UNRECOGNISED_MODEL in ledger


def test_a_vendor_name_that_is_not_an_identifier_is_not_stored(
    tmp_path: Path,
) -> None:
    """The vendor id is closed in the same way."""
    meter = _meter(tmp_path)

    meter.record("Some Vendor!", MODEL, _prompt(1))

    ledger = (tmp_path / LEDGER_RELPATH).read_text(encoding="utf-8")
    assert "Some Vendor!" not in ledger


# --------------------------------------------------------------------------
# The process-wide hook the boundary calls.
# --------------------------------------------------------------------------


def test_no_meter_is_active_outside_the_runtime() -> None:
    """Self-hosted callers and tests meter nothing unless a runtime installs it."""
    assert active_pool_meter() is None


def test_the_hook_is_inert_without_a_meter(tmp_path: Path) -> None:
    """Calling the hook with no meter installed writes nothing anywhere."""
    meter_answer(
        PoolCallKind.GENERATION,
        VENDOR,
        _body(),
        status=200,
        payload={"usage": {"prompt_tokens": 1}},
    )
    meter_unanswered(PoolCallKind.GENERATION, VENDOR, _body())

    assert not any(tmp_path.iterdir())


def test_the_context_installs_and_removes_the_meter(tmp_path: Path) -> None:
    """The runtime holds the meter for exactly the serving block."""
    with pool_spend_meter(tmp_path, prices=_pinned(), clock=Clock()) as meter:
        assert active_pool_meter() is meter
    assert active_pool_meter() is None


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_a_refused_answer_is_not_counted(tmp_path: Path, status: int) -> None:
    """A vendor refusal bills nothing, so it is not metered."""
    with pool_spend_meter(tmp_path, prices=_pinned(), clock=Clock()):
        meter_answer(
            PoolCallKind.GENERATION,
            VENDOR,
            _body(),
            status=status,
            payload={"error": {"code": "x"}},
        )

    assert not (tmp_path / LEDGER_RELPATH).exists()


def test_an_unanswered_dispatched_call_is_counted_as_an_estimate(
    tmp_path: Path,
) -> None:
    """A body the vendor may have billed is counted, conservatively."""
    with pool_spend_meter(tmp_path, prices=_pinned(), clock=Clock()) as meter:
        meter_unanswered(PoolCallKind.GENERATION, VENDOR, _body())
        (spend,) = meter.report()

    assert spend.estimated_calls == 1
    assert spend.completion_tokens == UNANSWERED_COMPLETION_ESTIMATE


@pytest.mark.parametrize(
    "breaker",
    [
        pytest.param(
            lambda vault: (vault / "00-Creek-Meta").write_text("x"), id="file"
        ),
    ],
)
def test_a_meter_failure_never_fails_the_call(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    breaker: Callable[[Path], object],
) -> None:
    """The answer is already billed; a ledger that cannot be written is logged."""
    caplog.set_level(logging.INFO, logger=pool_meter.TELEMETRY_LOGGER)
    breaker(tmp_path)

    with pool_spend_meter(tmp_path, prices=_pinned(), clock=Clock()):
        meter_answer(
            PoolCallKind.GENERATION,
            VENDOR,
            _body(),
            status=200,
            payload={"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    assert '"status": "meter_unavailable"' in caplog.text


def test_summary_lines_are_closed_key_value_pairs(tmp_path: Path) -> None:
    """The ``--check pool`` line is counts, money and closed labels only."""
    meter = _meter(tmp_path)
    meter.record(VENDOR, MODEL, PoolUsage(1_000, 2_000, 0))

    (line,) = (spend.summary_line() for spend in meter.report())

    assert line == (
        "pool-spend vendor=examplevendor month=2026-11 state=under-threshold "
        "prompt_tokens=1000 completion_tokens=2000 embedding_tokens=0 "
        "calls=1 estimated_calls=0 spend=0.01 threshold=5.00 cap=10.00 currency=EUR"
    )


def test_a_no_usage_line_names_the_month(tmp_path: Path) -> None:
    """A vault that has not dialled this month says so."""
    (line,) = (spend.summary_line() for spend in _meter(tmp_path).report())

    assert line == "pool-spend month=2026-11 state=no-usage"


def test_a_ledger_that_cannot_be_read_at_all_is_unreadable(tmp_path: Path) -> None:
    """An I/O failure reading the ledger is the same refusal, not a zero."""
    (tmp_path / LEDGER_RELPATH).mkdir(parents=True)

    (line,) = _meter(tmp_path).report()

    assert line.state is SpendState.LEDGER_UNREADABLE
    assert line.summary_line() == "pool-spend month=2026-11 state=ledger-unreadable"
