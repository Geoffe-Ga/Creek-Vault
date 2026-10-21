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
    R2_MACHINE_WAKE_ALLOWANCE_SECONDS,
    R2_POOL_CONCURRENCY,
    R2_POOL_GENERATE_P95_SECONDS,
    R2_POOL_PROMPT_TOKENS,
    R2_POOL_TTFT_P95_SECONDS,
    R2_VAULT_P95_SECONDS,
    BudgetName,
    BudgetRow,
    BudgetSource,
    BudgetTable,
    Verdict,
    budget_table,
)
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.runner import run_bench
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.httpapi.middleware import limits
from tests.bench.conftest import FakeModel, make_plan

if TYPE_CHECKING:
    from creek_mcp.bench.report import BenchReport

_ON_HARNESS_HOST = True
"""``host.describes_model_host`` for a hermetic run: the model ran here."""


def _trial(
    *,
    latency_s: float = 1.0,
    generation_s: float | None = 0.5,
    ttft_s: float | None = None,
    sweep: Sweep = Sweep.COLD_WARM,
    phase: Phase = Phase.WARM,
    concurrency: int = 1,
    outcome: Outcome = Outcome.OK,
    prompt_tokens: int | None = None,
    token_basis: TokenBasis | None = None,
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
        prompt_tokens=prompt_tokens,
        output_tokens=None,
        max_output_tokens=None,
        token_basis=token_basis,
    )


def _pool_trial(
    *,
    latency_s: float = 1.0,
    generation_s: float | None = 0.5,
    ttft_s: float | None = None,
    outcome: Outcome = Outcome.OK,
    concurrency: int = R2_POOL_CONCURRENCY,
    prompt_tokens: int | None = R2_POOL_PROMPT_TOKENS,
) -> Trial:
    """A concurrency-sweep trial at R2's stated level *and* prompt size."""
    return _trial(
        sweep=Sweep.CONCURRENCY,
        concurrency=concurrency,
        latency_s=latency_s,
        generation_s=generation_s,
        ttft_s=ttft_s,
        outcome=outcome,
        prompt_tokens=prompt_tokens,
        token_basis=TokenBasis.PROVIDER_REPORTED,
    )


def _row(table: BudgetTable, name: BudgetName) -> BudgetRow:
    """Return the row named *name*."""
    return next(row for row in table.rows if row.name is name)


def test_every_r2_budget_appears_as_its_own_named_row() -> None:
    """All four R2 budgets plus the shed deadline are reported by name."""
    table = budget_table([_trial()], describes_model_host=_ON_HARNESS_HOST)
    assert {row.name for row in table.rows} == set(BudgetName)
    limits_by_name = {row.name: row.limit_seconds for row in table.rows}
    assert limits_by_name == {
        BudgetName.SERVER_SHED_DEADLINE: pytest.approx(limits.DEFAULT_TIMEOUT_SECONDS),
        BudgetName.VAULT_FIRST_BYTE_WINDOW: pytest.approx(
            ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS
        ),
        BudgetName.VAULT_REFLECTION_P95: pytest.approx(R2_VAULT_P95_SECONDS),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE: pytest.approx(
            R2_MACHINE_WAKE_ALLOWANCE_SECONDS
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
        budget_table(trials, describes_model_host=_ON_HARNESS_HOST),
        BudgetName.SERVER_SHED_DEADLINE,
    )
    assert row.source is BudgetSource.CODE
    assert row.limit_seconds == pytest.approx(5.0)
    assert row.verdict is Verdict.FITS

    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", 3.0)
    moved = _row(
        budget_table(trials, describes_model_host=_ON_HARNESS_HOST),
        BudgetName.SERVER_SHED_DEADLINE,
    )
    assert moved.limit_seconds == pytest.approx(3.0)
    assert moved.verdict is Verdict.EXCEEDS


def test_the_binding_budget_is_the_first_byte_window_until_adepthood_widens() -> None:
    """The 25 s row is reported but out of force; the 10 s window binds."""
    assert ADEPTHOOD_PHASE_BUDGETS_WIDENED is False
    table = budget_table([_trial()], describes_model_host=_ON_HARNESS_HOST)
    assert table.binding_budget is BudgetName.VAULT_FIRST_BYTE_WINDOW
    assert table.phase_budgets_widened is False
    assert _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW).in_force is True
    assert _row(table, BudgetName.VAULT_REFLECTION_P95).in_force is False


def test_a_green_widened_row_cannot_carry_the_table_verdict() -> None:
    """Inside 25 s but past the 10 s window in force: the table exceeds."""
    table = budget_table(
        [_trial(latency_s=18.0)], describes_model_host=_ON_HARNESS_HOST
    )
    assert _row(table, BudgetName.VAULT_REFLECTION_P95).verdict is Verdict.FITS
    assert _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW).verdict is Verdict.EXCEEDS
    assert table.verdict is Verdict.EXCEEDS


def test_one_timed_out_trial_exceeds_every_budget_it_was_inside() -> None:
    """The error-rate clause, not the p95, decides a group with a failure."""
    trials = [_trial(latency_s=0.1), _trial(latency_s=0.0, outcome=Outcome.TIMEOUT)]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    inside = _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW)
    assert inside.measured_seconds == pytest.approx(0.1)
    assert inside.measured_seconds is not None
    assert inside.measured_seconds < ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS
    assert inside.verdict is Verdict.EXCEEDS
    assert table.verdict is Verdict.EXCEEDS


def test_the_pool_row_is_judged_at_r2s_concurrency_level() -> None:
    """Level 4's generate p95 is the row; other levels do not feed it."""
    slower_level = _pool_trial(concurrency=1, generation_s=99.0)
    at_four = [_pool_trial(generation_s=3.0) for _ in range(R2_POOL_CONCURRENCY)]
    table = budget_table(
        [slower_level, *at_four], describes_model_host=_ON_HARNESS_HOST
    )
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert table.pool_concurrency_level == R2_POOL_CONCURRENCY
    assert table.pool_level_measured is True
    assert table.pool_prompt_size_measured is True
    assert row.measured_seconds == pytest.approx(3.0)
    assert row.verdict is Verdict.FITS


def test_the_report_says_so_when_level_four_was_never_requested() -> None:
    """No level-4 trials: the row is insufficient data, not a pass."""
    trials = [_trial(sweep=Sweep.CONCURRENCY, concurrency=2, generation_s=0.1)]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert table.pool_level_measured is False
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_a_non_streaming_backend_leaves_the_ttft_row_unmeasured() -> None:
    """No first-token report is a gap, never a pass and never a failure."""
    table = budget_table(
        [_pool_trial(ttft_s=None)], describes_model_host=_ON_HARNESS_HOST
    )
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
    table = budget_table(
        [_pool_trial(ttft_s=ttft_s)], describes_model_host=_ON_HARNESS_HOST
    )
    row = _row(table, BudgetName.POOL_TTFT_P95)
    assert row.measured_seconds == pytest.approx(ttft_s)
    assert row.verdict is expected


def test_the_wake_row_is_judged_on_the_wake_bearing_trials() -> None:
    """Cold and resume trials carry the wake; warm ones do not."""
    trials = [
        _trial(phase=Phase.WARM, latency_s=1.0),
        _trial(phase=Phase.COLD, latency_s=3.0),
        _trial(phase=Phase.RESUME, sweep=Sweep.IDLE, latency_s=4.0),
    ]
    row = _row(
        budget_table(trials, describes_model_host=_ON_HARNESS_HOST),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
    )
    assert row.measured_seconds == pytest.approx(4.0)
    assert row.verdict is Verdict.FITS


def test_the_wake_allowance_is_what_is_left_beside_the_vault_budget() -> None:
    """R2 puts the wake *beside* 25 s inside 30 s, so 5 s is left for it."""
    allowance = R2_MACHINE_WAKE_ALLOWANCE_SECONDS
    beside_the_vault_budget = ADEPTHOOD_TOTAL_DEADLINE_SECONDS - R2_VAULT_P95_SECONDS
    assert allowance == pytest.approx(beside_the_vault_budget)


def test_a_wake_that_leaves_the_reflection_no_room_does_not_fit() -> None:
    """A 14 s wake-bearing trial cannot read "fits inside the deadline".

    It is inside adepthood's 30 s total only by spending the 25 s the
    reflection itself is budgeted. Judged against the full 30 s this row read
    ``fits`` at 14 s, which is the claim R2 does not support.
    """
    row = _row(
        budget_table(
            [_trial(phase=Phase.COLD, latency_s=14.0)],
            describes_model_host=_ON_HARNESS_HOST,
        ),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
    )
    assert row.measured_seconds == pytest.approx(14.0)
    assert row.measured_seconds is not None
    assert row.measured_seconds < ADEPTHOOD_TOTAL_DEADLINE_SECONDS
    assert row.verdict is Verdict.EXCEEDS


def test_the_wake_row_is_unmeasured_without_a_cold_or_resumed_trial() -> None:
    """An all-warm run measured no wake, so it claims nothing about one."""
    row = _row(
        budget_table([_trial(phase=Phase.WARM)], describes_model_host=_ON_HARNESS_HOST),
        BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
    )
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_an_empty_run_measures_no_budget() -> None:
    """Nothing measured is insufficient data everywhere, never a pass."""
    table = budget_table([], describes_model_host=_ON_HARNESS_HOST)
    assert all(row.measured_seconds is None for row in table.rows)
    assert {row.verdict for row in table.rows} == {Verdict.INSUFFICIENT_DATA}
    assert table.verdict is Verdict.INSUFFICIENT_DATA


@pytest.mark.parametrize("describes_model_host", [True, False])
def test_the_off_host_field_is_the_exact_complement_of_the_host_fact(
    *, describes_model_host: bool
) -> None:
    """One fact, recorded once: the two fields can never disagree."""
    table = budget_table([_trial()], describes_model_host=describes_model_host)
    assert table.model_off_harness_host is not describes_model_host


def test_the_table_claims_no_production_path_at_all() -> None:
    """No field licenses a production reading, so none can be misquoted.

    The table used to carry ``measures_production_path``, which was true for
    any private or remote endpoint — including a LAN dev box reached by
    ``bench --mode live --ollama-url http://192.168.1.50:11434``, which dials
    no pool, wakes no Machine and includes no attestation. The field is gone
    rather than redefined: no run this harness can execute today measures the
    production path, so there is nothing for such a field to be true of.
    """
    assert not [
        name for name in BudgetTable.model_fields if "production" in name.lower()
    ]


def test_a_private_endpoint_licenses_no_production_claim() -> None:
    """Reaching a model off this host is not reaching production.

    ``describes_model_host=False`` is what a private or remote endpoint
    produces. It moves exactly one narrow field and no verdict, limit or
    source anywhere in the table.
    """
    off_host = budget_table([_trial()], describes_model_host=False)
    on_host = budget_table([_trial()], describes_model_host=True)
    assert off_host.model_off_harness_host is True
    assert off_host.verdict is on_host.verdict
    assert off_host.rows == on_host.rows
    assert off_host.binding_budget is on_host.binding_budget


def test_the_table_carries_the_source_of_every_row() -> None:
    """A reader can tell a code-derived budget from an ADR-stated one."""
    table = budget_table([_trial()], describes_model_host=_ON_HARNESS_HOST)
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
    trials = [_pool_trial(generation_s=None)]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
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
            prompt_tokens=R2_POOL_PROMPT_TOKENS,
            token_basis=TokenBasis.PROVIDER_REPORTED,
        )
    ]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    assert table.pool_level_measured is False
    assert _row(table, BudgetName.POOL_GENERATE_P95).measured_seconds is None


def _fake_report(model: FakeModel | None = None) -> BenchReport:
    """Run the hermetic fake backend across every sweep, including level 4."""
    with tempfile.TemporaryDirectory() as tmp:
        plan = make_plan(
            model if model is not None else FakeModel(),
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
    assert report.budgets.model_off_harness_host is False
    assert report.host.describes_model_host is True
    assert report.budgets.binding_budget is BudgetName.VAULT_FIRST_BYTE_WINDOW
    assert report.budgets.phase_budgets_widened is False


def test_the_report_reaches_r2s_level_four_when_the_sweep_requests_it() -> None:
    """R2 states its pool budget at concurrency 4, so the sweep reports it."""
    budgets = _fake_report().budgets
    assert budgets.pool_level_measured is True
    assert budgets.pool_concurrency_level == R2_POOL_CONCURRENCY


def test_a_fake_run_meets_r2s_level_but_not_its_prompt_size() -> None:
    """R2 states two conditions; the harness discloses both, separately.

    The fake has no tokenizer, so no trial carries a provider-reported prompt
    count and the 2,500-token condition is unmet. The row is a gap rather
    than a pass earned on an eight-word query.
    """
    budgets = _fake_report().budgets
    assert budgets.pool_level_measured is True
    assert budgets.pool_prompt_size_measured is False
    for name in (BudgetName.POOL_GENERATE_P95, BudgetName.POOL_TTFT_P95):
        row = _row(budgets, name)
        assert row.measured_seconds is None, name
        assert row.verdict is Verdict.INSUFFICIENT_DATA, name


def test_a_prompt_nowhere_near_r2s_size_does_not_answer_its_budget() -> None:
    """Level 4 at a 64-word default query is not the row R2 states."""
    trials = [_pool_trial(prompt_tokens=80, generation_s=1.0)]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    assert table.pool_level_measured is True
    assert table.pool_prompt_size_measured is False
    row = _row(table, BudgetName.POOL_GENERATE_P95)
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_a_byte_upper_bound_prompt_count_is_not_a_measurement() -> None:
    """Only a provider-reported count can answer R2's prompt condition."""
    trials = [
        _trial(
            sweep=Sweep.CONCURRENCY,
            concurrency=R2_POOL_CONCURRENCY,
            generation_s=1.0,
            prompt_tokens=R2_POOL_PROMPT_TOKENS,
            token_basis=TokenBasis.BYTE_UPPER_BOUND,
        )
    ]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    assert table.pool_prompt_size_measured is False
    assert _row(table, BudgetName.POOL_GENERATE_P95).measured_seconds is None


def test_the_concurrency_sweep_does_not_set_the_vault_figure() -> None:
    """Trials run simultaneously carry each other's queueing delay.

    ``_concurrency`` puts ``level`` reflections in flight together and every
    ``latency_s`` is wall clock, so a level-4 trial's latency includes the
    wait behind its three siblings. R2 budgets a *single* cold or warm
    reflection, so letting contention set ``vault_reflection_p95`` would
    publish a load figure as the production one — the same reasoning that
    excludes the context sweep.
    """
    trials = [
        _trial(sweep=Sweep.COLD_WARM, latency_s=2.0),
        _pool_trial(latency_s=22.0),
    ]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    for name in (BudgetName.VAULT_FIRST_BYTE_WINDOW, BudgetName.VAULT_REFLECTION_P95):
        row = _row(table, name)
        assert row.measured_seconds == pytest.approx(2.0), name
        assert row.verdict is Verdict.FITS, name


def test_a_context_sweep_failure_does_not_fail_the_ttft_row() -> None:
    """The error-rate clause is scoped to the group the row describes.

    Judging the TTFT row over every trial let one deliberate out-of-memory in
    the context sweep — whose whole purpose is to find where the window
    breaks — publish a row reading "limit 2.0 s, measured 1.0 s, verdict
    exceeds".
    """
    trials = [
        _pool_trial(ttft_s=1.0, generation_s=1.0),
        _trial(sweep=Sweep.CONTEXT, latency_s=30.0, outcome=Outcome.OOM),
    ]
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    for name in (BudgetName.POOL_TTFT_P95, BudgetName.POOL_GENERATE_P95):
        row = _row(table, name)
        assert row.verdict is Verdict.FITS, name
    assert _row(table, BudgetName.POOL_TTFT_P95).measured_seconds == pytest.approx(1.0)


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
    table = budget_table(trials, describes_model_host=_ON_HARNESS_HOST)
    for name in (BudgetName.VAULT_FIRST_BYTE_WINDOW, BudgetName.VAULT_REFLECTION_P95):
        assert _row(table, name).measured_seconds == pytest.approx(2.0), name


def test_the_shed_deadline_is_judged_over_every_trial() -> None:
    """The deadline this process sheds on sheds context trials too.

    Scoping this row to the representative reflection let it read ``fits`` at
    4 s beside a ``per_sweep.context`` verdict of ``exceeds`` for the *same*
    number — and this is the row a reader quotes, because it is the one whose
    source is this repository's code.
    """
    trials = [
        _trial(sweep=Sweep.COLD_WARM, latency_s=4.0),
        _trial(sweep=Sweep.CONTEXT, latency_s=35.0),
    ]
    row = _row(
        budget_table(trials, describes_model_host=_ON_HARNESS_HOST),
        BudgetName.SERVER_SHED_DEADLINE,
    )
    assert row.measured_seconds == pytest.approx(35.0)
    assert row.verdict is Verdict.EXCEEDS


def test_a_context_only_run_measures_no_vault_budget() -> None:
    """With nothing but stress trials, the vault rows claim nothing."""
    table = budget_table(
        [_trial(sweep=Sweep.CONTEXT, latency_s=1.0)],
        describes_model_host=_ON_HARNESS_HOST,
    )
    row = _row(table, BudgetName.VAULT_FIRST_BYTE_WINDOW)
    assert row.measured_seconds is None
    assert row.verdict is Verdict.INSUFFICIENT_DATA


def test_a_streaming_backend_reaches_trial_ttft_through_the_runner() -> None:
    """The runner wires ``probe.ttft_s`` onto every trial it produces.

    Every other TTFT test asserts the ``None`` a non-streaming backend
    produces, so all of them stay green if the runner hard-codes
    ``ttft_s=None``. This one drives ``run_bench`` — and so ``run_trial`` —
    with a double that *does* report a first-token time, which is the only
    way that mutation dies.
    """
    report = _fake_report(FakeModel(reports_ttft=0.25))
    trials = _all_trials(report)
    assert trials
    assert all(trial.ttft_s == pytest.approx(0.25) for trial in trials)
    for summary in report.per_sweep.values():
        assert summary.latency.ttft_p95_s == pytest.approx(0.25)


def test_a_negative_first_token_time_is_unknown_not_instant() -> None:
    """A skewed clock is not a measurement, and never "instant".

    Clamping to ``0.0`` would publish the one thing :attr:`Trial.ttft_s`
    promises ``None`` never means. ``None`` is the honest clamp.
    """
    trials = _all_trials(_fake_report(FakeModel(reports_ttft=-0.4)))
    assert trials
    assert all(trial.ttft_s is None for trial in trials)


def test_no_shipped_backend_answers_the_ttft_row() -> None:
    """The row is in force and structurally unreachable, so it says so.

    Neither ``ollama_client`` nor ``fake`` implements
    :class:`~creek_mcp.bench.protocols.FirstTokenReporting`'s streaming
    figure, so ``fits`` cannot be reached in any configuration shipped today.
    The row stays in force and reports the gap rather than being quietly
    dropped; #1899's pool client is what will answer it.
    """
    row = _row(_fake_report().budgets, BudgetName.POOL_TTFT_P95)
    assert row.in_force is True
    assert row.verdict is Verdict.INSUFFICIENT_DATA
