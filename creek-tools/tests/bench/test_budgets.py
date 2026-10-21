"""ADR-0016 R2's named budget table, and what a figure in it may be read as.

Each row carries its own limit, the value it was judged against and its own
verdict. Two properties matter more than any single row:

- a budget the code states moves when the code moves (the shed deadline);
- a budget production does **not** enforce today is reported with
  ``in_force`` false and is left out of the table's verdict, so a green 25 s
  row can never be read as "production is fine".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek_mcp.bench.deadline import (
    ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS,
    ADEPTHOOD_PHASE_BUDGETS_WIDENED,
    ADEPTHOOD_TOTAL_DEADLINE_SECONDS,
    R2_POOL_CONCURRENCY,
    R2_POOL_GENERATE_P95_SECONDS,
    R2_POOL_TTFT_P95_SECONDS,
    R2_VAULT_P95_SECONDS,
    BudgetName,
    BudgetRow,
    BudgetSource,
    BudgetTable,
    Verdict,
    budget_table,
    measures_production_path,
)
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.runner import run_bench
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.httpapi.middleware import limits
from tests.bench.conftest import FakeModel, make_plan

if TYPE_CHECKING:
    from creek_mcp.bench.report import BenchReport

_FAKE_SCOPE = "fake"


def _trial(
    *,
    latency_s: float = 1.0,
    generation_s: float | None = 0.5,
    ttft_s: float | None = None,
    sweep: Sweep = Sweep.COLD_WARM,
    phase: Phase = Phase.WARM,
    concurrency: int = 1,
    outcome: Outcome = Outcome.OK,
) -> Trial:
    """One trial, defaulting to a fast warm success."""
    return Trial(
        sweep=sweep,
        phase=phase,
        concurrency=concurrency,
        input_words=4,
        latency_s=latency_s,
        generation_s=generation_s,
        ttft_s=ttft_s,
        outcome=outcome,
        model_resident_bytes=None,
        prompt_tokens=None,
        output_tokens=None,
        max_output_tokens=None,
        token_basis=None,
    )


def _row(table: BudgetTable, name: BudgetName) -> BudgetRow:
    """Return the row named *name*."""
    return next(row for row in table.rows if row.name is name)


def test_every_r2_budget_appears_as_its_own_named_row() -> None:
    """All four R2 budgets plus the shed deadline are reported by name."""
    table = budget_table([_trial()], endpoint_scope=_FAKE_SCOPE)
    assert {row.name for row in table.rows} == set(BudgetName)
    limits_by_name = {row.name: row.limit_seconds for row in table.rows}
    assert limits_by_name == {
        BudgetName.SERVER_SHED_DEADLINE: pytest.approx(limits.DEFAULT_TIMEOUT_SECONDS),
        BudgetName.VAULT_FIRST_BYTE_WINDOW: pytest.approx(
            ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS
        ),
        BudgetName.VAULT_REFLECTION_P95: pytest.approx(R2_VAULT_P95_SECONDS),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE: pytest.approx(
            ADEPTHOOD_TOTAL_DEADLINE_SECONDS
        ),
        BudgetName.POOL_GENERATE_P95: pytest.approx(R2_POOL_GENERATE_P95_SECONDS),
        BudgetName.POOL_TTFT_P95: pytest.approx(R2_POOL_TTFT_P95_SECONDS),
    }


def test_the_shed_deadline_moves_with_the_code_that_states_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching the server's timeout moves the row's limit and its verdict."""
    trials = [_trial(latency_s=4.0)]
    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", 5.0)
    row = _row(
        budget_table(trials, endpoint_scope=_FAKE_SCOPE),
        BudgetName.SERVER_SHED_DEADLINE,
    )
    assert row.source is BudgetSource.CODE
    assert row.limit_seconds == pytest.approx(5.0)
    assert row.verdict is Verdict.FITS

    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", 3.0)
    moved = _row(
        budget_table(trials, endpoint_scope=_FAKE_SCOPE),
        BudgetName.SERVER_SHED_DEADLINE,
    )
    assert moved.limit_seconds == pytest.approx(3.0)
    assert moved.verdict is Verdict.EXCEEDS


def test_the_binding_budget_is_the_first_byte_window_until_adepthood_widens() -> None:
    """The 25 s row is reported but out of force; the 10 s window binds."""
    assert ADEPTHOOD_PHASE_BUDGETS_WIDENED is False
    table = budget_table([_trial()], endpoint_scope=_FAKE_SCOPE)
    assert table.binding_budget is BudgetName.VAULT_FIRST_BYTE_WINDOW
    assert table.phase_budgets_widened is False
    assert _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW).in_force is True
    assert _row(table, BudgetName.VAULT_REFLECTION_P95).in_force is False


def test_a_green_widened_row_cannot_carry_the_table_verdict() -> None:
    """Inside 25 s but past the 10 s window in force: the table exceeds."""
    table = budget_table([_trial(latency_s=18.0)], endpoint_scope=_FAKE_SCOPE)
    assert _row(table, BudgetName.VAULT_REFLECTION_P95).verdict is Verdict.FITS
    assert _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW).verdict is Verdict.EXCEEDS
    assert table.verdict is Verdict.EXCEEDS


def test_one_timed_out_trial_exceeds_every_budget_it_was_inside() -> None:
    """The error-rate clause, not the p95, decides a group with a failure."""
    trials = [_trial(latency_s=0.1), _trial(latency_s=0.0, outcome=Outcome.TIMEOUT)]
    table = budget_table(trials, endpoint_scope=_FAKE_SCOPE)
    inside = _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW)
    assert inside.measured_seconds == pytest.approx(0.1)
    assert inside.measured_seconds is not None
    assert inside.measured_seconds < ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS
    assert inside.verdict is Verdict.EXCEEDS
    assert table.verdict is Verdict.EXCEEDS


def test_the_pool_row_is_judged_at_r2s_concurrency_level() -> None:
    """Level 4's generate p95 is the row; other levels do not feed it."""
    slower_level = _trial(sweep=Sweep.CONCURRENCY, concurrency=1, generation_s=99.0)
    at_four = [
        _trial(
            sweep=Sweep.CONCURRENCY,
            concurrency=R2_POOL_CONCURRENCY,
            generation_s=3.0,
        )
        for _ in range(R2_POOL_CONCURRENCY)
    ]
    table = budget_table([slower_level, *at_four], endpoint_scope=_FAKE_SCOPE)
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert table.pool_concurrency_level == R2_POOL_CONCURRENCY
    assert table.pool_level_measured is True
    assert row.measured_seconds == pytest.approx(3.0)
    assert row.verdict is Verdict.FITS


def test_the_report_says_so_when_level_four_was_never_requested() -> None:
    """No level-4 trials: the row is insufficient data, not a pass."""
    trials = [_trial(sweep=Sweep.CONCURRENCY, concurrency=2, generation_s=0.1)]
    table = budget_table(trials, endpoint_scope=_FAKE_SCOPE)
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert table.pool_level_measured is False
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_a_non_streaming_backend_leaves_the_ttft_row_unmeasured() -> None:
    """No first-token report is a gap, never a pass and never a failure."""
    table = budget_table([_trial(ttft_s=None)], endpoint_scope=_FAKE_SCOPE)
    row = _row(table, BudgetName.POOL_TTFT_P95)
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA
    assert table.verdict is Verdict.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    ("ttft_s", "expected"),
    [(1.5, Verdict.FITS), (2.0, Verdict.FITS), (2.5, Verdict.EXCEEDS)],
)
def test_a_streaming_backend_is_judged_against_the_two_second_budget(
    ttft_s: float, expected: Verdict
) -> None:
    """A measured TTFT past 2 s exceeds; at or inside it fits."""
    table = budget_table([_trial(ttft_s=ttft_s)], endpoint_scope=_FAKE_SCOPE)
    row = _row(table, BudgetName.POOL_TTFT_P95)
    assert row.measured_seconds == pytest.approx(ttft_s)
    assert row.verdict is expected


def test_the_wake_row_is_judged_on_the_wake_bearing_trials() -> None:
    """Cold and resume trials carry the wake; warm ones do not."""
    trials = [
        _trial(phase=Phase.WARM, latency_s=1.0),
        _trial(phase=Phase.COLD, latency_s=12.0),
        _trial(phase=Phase.RESUME, sweep=Sweep.IDLE, latency_s=14.0),
    ]
    row = _row(
        budget_table(trials, endpoint_scope=_FAKE_SCOPE),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
    )
    assert row.measured_seconds == pytest.approx(14.0)
    assert row.verdict is Verdict.FITS


def test_the_wake_row_is_unmeasured_without_a_cold_or_resumed_trial() -> None:
    """An all-warm run measured no wake, so it claims nothing about one."""
    row = _row(
        budget_table([_trial(phase=Phase.WARM)], endpoint_scope=_FAKE_SCOPE),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
    )
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_an_empty_run_measures_no_budget() -> None:
    """Nothing measured is insufficient data everywhere, never a pass."""
    table = budget_table([], endpoint_scope=_FAKE_SCOPE)
    assert all(row.measured_seconds is None for row in table.rows)
    assert {row.verdict for row in table.rows} == {Verdict.INSUFFICIENT_DATA}
    assert table.verdict is Verdict.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("fake", False), ("loopback", False), ("private", True), ("remote", True)],
)
def test_only_a_run_that_reached_a_real_endpoint_describes_production(
    scope: str, expected: bool
) -> None:
    """A fake or loopback run measures the harness floor, not the pool."""
    assert measures_production_path(scope) is expected
    table = budget_table([_trial()], endpoint_scope=scope)
    assert table.measures_production_path is expected


def test_the_table_carries_the_source_of_every_row() -> None:
    """A reader can tell a code-derived budget from an ADR-stated one."""
    table = budget_table([_trial()], endpoint_scope=_FAKE_SCOPE)
    sources = {row.name: row.source for row in table.rows}
    assert sources == {
        BudgetName.SERVER_SHED_DEADLINE: BudgetSource.CODE,
        BudgetName.VAULT_FIRST_BYTE_WINDOW: BudgetSource.DOCUMENTED_CONSTANT,
        BudgetName.VAULT_REFLECTION_P95: BudgetSource.ADR_0016_R2,
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE: (
            BudgetSource.DOCUMENTED_CONSTANT
        ),
        BudgetName.POOL_GENERATE_P95: BudgetSource.ADR_0016_R2,
        BudgetName.POOL_TTFT_P95: BudgetSource.ADR_0016_R2,
    }


def test_a_level_four_trial_that_never_reached_the_model_is_unmeasured() -> None:
    """Level 4 ran but produced no generate figure: a gap, not a pass."""
    trials = [
        _trial(
            sweep=Sweep.CONCURRENCY,
            concurrency=R2_POOL_CONCURRENCY,
            generation_s=None,
        )
    ]
    table = budget_table(trials, endpoint_scope=_FAKE_SCOPE)
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert table.pool_level_measured is True
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_a_concurrency_level_of_four_outside_the_sweep_does_not_feed_the_row() -> None:
    """Only the concurrency sweep answers R2's level-4 pool budget."""
    trials = [
        _trial(
            sweep=Sweep.COLD_WARM,
            concurrency=R2_POOL_CONCURRENCY,
            generation_s=1.0,
        )
    ]
    table = budget_table(trials, endpoint_scope=_FAKE_SCOPE)
    assert table.pool_level_measured is False
    assert _row(table, BudgetName.POOL_GENERATE_P95).measured_seconds is None


def _fake_report() -> BenchReport:
    """Run the hermetic fake backend across every sweep, including level 4."""
    with tempfile.TemporaryDirectory() as tmp:
        plan = make_plan(
            FakeModel(),
            seed=5,
            entries=3,
            words_per_entry=8,
            query_words=8,
            cold_trials=1,
            warm_trials=2,
            context_sizes=(12,),
            concurrency_levels=(1, R2_POOL_CONCURRENCY),
            idle_cycles=1,
            measure_tokens=True,
        )
        return run_bench(plan, Path(tmp) / "corpus")


def test_a_fake_mode_report_carries_p50_p95_and_ttft_per_sweep_and_phase() -> None:
    """Every sweep and phase reports the four latency figures R2 needs."""
    report = _fake_report()
    assert report.per_sweep
    for summary in report.per_sweep.values():
        groups = [summary.latency, *(p.latency for p in summary.per_phase.values())]
        assert summary.per_phase
        for latency in groups:
            assert latency.p50_s is not None
            assert latency.p95_s is not None
            assert latency.generation_p95_s is not None
            assert latency.ttft_p95_s is None


def test_a_fake_run_states_no_throughput_and_says_why() -> None:
    """The fake has no tokenizer, so its rate is refused and labelled."""
    report = _fake_report()
    for summary in report.per_sweep.values():
        blocks = [
            summary.throughput,
            *(phase.throughput for phase in summary.per_phase.values()),
        ]
        for throughput in blocks:
            assert throughput.basis is TokenBasis.BYTE_UPPER_BOUND
            assert throughput.count == 0
            assert throughput.excluded_count > 0
            assert throughput.tokens_per_second_p50 is None
            assert throughput.tokens_per_second_p95 is None


def test_a_non_streaming_fake_run_still_produces_a_valid_report() -> None:
    """No TTFT and no tokens/s invalidates nothing and fails no sweep."""
    report = _fake_report()
    assert all(trial.ttft_s is None for trial in _all_trials(report))
    assert report.verdict is Verdict.FITS
    assert _row(report.budgets, BudgetName.POOL_TTFT_P95).verdict is (
        Verdict.INSUFFICIENT_DATA
    )


def test_a_fake_mode_report_never_claims_to_describe_production() -> None:
    """A hermetic run measures this host; the report says so in one field."""
    report = _fake_report()
    assert report.budgets.measures_production_path is False
    assert report.budgets.binding_budget is BudgetName.VAULT_FIRST_BYTE_WINDOW
    assert report.budgets.phase_budgets_widened is False


def test_the_report_reaches_r2s_level_four_when_the_sweep_requests_it() -> None:
    """R2 states its pool budget at concurrency 4, so the sweep reports it."""
    budgets = _fake_report().budgets
    assert budgets.pool_level_measured is True
    assert budgets.pool_concurrency_level == R2_POOL_CONCURRENCY
    assert _row(budgets, BudgetName.POOL_GENERATE_P95).measured_seconds is not None


def test_the_sweep_verdict_and_the_budget_verdict_are_reported_apart() -> None:
    """A run inside the shed deadline does not thereby answer R2."""
    report = _fake_report()
    assert report.verdict is Verdict.FITS
    assert report.budgets.verdict is Verdict.INSUFFICIENT_DATA


def _all_trials(report: BenchReport) -> list[Trial]:
    """Return every trial in *report*, across all sweeps."""
    return [trial for sweep in report.per_sweep.values() for trial in sweep.trials]


def test_the_context_stress_sweep_does_not_set_the_vault_figure() -> None:
    """A deliberately oversized entry is not the reflection R2 budgets.

    The context sweep walks input size up towards ``num_ctx`` to find where
    the window breaks. Letting its p95 name itself ``vault_reflection_p95``
    would publish a stress result as the production figure.
    """
    trials = [
        _trial(sweep=Sweep.COLD_WARM, latency_s=2.0),
        _trial(sweep=Sweep.CONTEXT, latency_s=24.0),
    ]
    table = budget_table(trials, endpoint_scope=_FAKE_SCOPE)
    for name in (
        BudgetName.SERVER_SHED_DEADLINE,
        BudgetName.VAULT_FIRST_BYTE_WINDOW,
        BudgetName.VAULT_REFLECTION_P95,
    ):
        assert _row(table, name).measured_seconds == pytest.approx(2.0), name


def test_a_context_only_run_measures_no_vault_budget() -> None:
    """With nothing but stress trials, the vault rows claim nothing."""
    table = budget_table(
        [_trial(sweep=Sweep.CONTEXT, latency_s=1.0)], endpoint_scope=_FAKE_SCOPE
    )
    row = _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW)
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA
