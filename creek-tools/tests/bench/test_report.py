"""Report assembly: verdict combination and the admission-cap capacity basis."""

from __future__ import annotations

import pytest

from creek_mcp.bench.deadline import Verdict
from creek_mcp.bench.metadata import HostMetadata
from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.report import build_report, capacity_basis, overall_verdict
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.httpapi.middleware import limits
from tests.bench.conftest import run_metadata

_BUDGET = 10.0
_HOST = HostMetadata(
    describes_model_host=True,
    disk_scope="harness_temp",
    cpu_count=2,
    cpu_arch="x86_64",
    cpu_kind="shared",
    kernel_release="6.1.0",
    ram_bytes=1024,
    disk_free_bytes=1024,
)


def _trial(
    level: int,
    latency_s: float = 1.0,
    outcome: Outcome = Outcome.OK,
    sweep: Sweep = Sweep.CONCURRENCY,
) -> Trial:
    """One trial at concurrency *level*."""
    return Trial(
        sweep=sweep,
        phase=Phase.WARM,
        concurrency=level,
        input_words=4,
        latency_s=latency_s,
        generation_s=None,
        outcome=outcome,
        model_resident_bytes=None,
    )


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], Verdict.INSUFFICIENT_DATA),
        ([Verdict.FITS, Verdict.FITS], Verdict.FITS),
        ([Verdict.FITS, Verdict.INSUFFICIENT_DATA], Verdict.INSUFFICIENT_DATA),
        ([Verdict.INSUFFICIENT_DATA, Verdict.EXCEEDS], Verdict.EXCEEDS),
        ([Verdict.FITS, Verdict.EXCEEDS], Verdict.EXCEEDS),
    ],
)
def test_overall_verdict(verdicts: list[Verdict], expected: Verdict) -> None:
    """A failure anywhere dominates; then any gap; else the envelope fits."""
    assert overall_verdict(verdicts) is expected


def test_capacity_basis_is_the_largest_contiguous_fitting_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Levels 1 and 2 fit, 4 is too slow: 8 fitting again does not count."""
    monkeypatch.setattr(limits, "DEFAULT_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(limits, "DEFAULT_MAX_PER_CONSUMER", 1)
    trials = (
        [_trial(1)]
        + [_trial(2)] * 2
        + [_trial(4, latency_s=_BUDGET + 1)] * 4
        + [_trial(8)] * 8
    )
    basis = capacity_basis(trials, _BUDGET)
    assert basis.max_fitting_concurrency == 2
    assert basis.admission_max_concurrency == 2
    assert basis.admission_max_per_consumer == 1
    assert basis.process_cap_within_capacity is True
    assert basis.per_consumer_cap_within_capacity is True


def test_capacity_basis_flags_caps_above_capacity() -> None:
    """With the shipped caps (32/8), fitting only level 4 is not enough."""
    trials = [_trial(4)] * 4 + [_trial(8, outcome=Outcome.OOM)] * 8
    basis = capacity_basis(trials, _BUDGET)
    assert basis.max_fitting_concurrency == 4
    assert basis.admission_max_concurrency == limits.DEFAULT_MAX_CONCURRENCY
    assert basis.process_cap_within_capacity is False
    assert basis.per_consumer_cap_within_capacity is False


def test_capacity_basis_when_nothing_fits() -> None:
    """If the smallest level fails, no level fits and no cap is within it."""
    basis = capacity_basis([_trial(1, outcome=Outcome.TIMEOUT)], _BUDGET)
    assert basis.max_fitting_concurrency is None
    assert basis.process_cap_within_capacity is False
    assert basis.per_consumer_cap_within_capacity is False


def test_capacity_basis_without_a_concurrency_sweep() -> None:
    """No measurement means no claim either way."""
    basis = capacity_basis([], _BUDGET)
    assert basis.max_fitting_concurrency is None
    assert basis.process_cap_within_capacity is None
    assert basis.per_consumer_cap_within_capacity is None


def test_build_report_groups_judges_and_omits_empty_sweeps() -> None:
    """Sweeps are judged separately; a sweep that never ran is absent."""
    trials = [
        _trial(1, sweep=Sweep.COLD_WARM),
        _trial(1, latency_s=1e9, sweep=Sweep.CONTEXT),
        _trial(1),
    ]
    report = build_report(run_metadata(), _HOST, trials)
    assert set(report.per_sweep) == {Sweep.COLD_WARM, Sweep.CONTEXT, Sweep.CONCURRENCY}
    assert report.per_sweep[Sweep.COLD_WARM].verdict is Verdict.FITS
    assert report.per_sweep[Sweep.CONTEXT].verdict is Verdict.EXCEEDS
    assert report.verdict is Verdict.EXCEEDS
    assert report.kind == "benchmark"
    assert report.quality_score is None
    assert report.capacity.max_fitting_concurrency == 1


def _phase_trial(phase: Phase, latency_s: float) -> Trial:
    """One ok cold/warm trial in *phase*."""
    return _trial(1, latency_s=latency_s, sweep=Sweep.COLD_WARM).model_copy(
        update={"phase": phase}
    )


def test_cold_start_is_judged_separately_from_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow cold trial cannot hide behind thirty fast warm ones.

    Pooled, nearest-rank p95 of 31 trials is rank 30, a warm trial, so the
    pooled summary fits. Per phase, the cold p95 is over budget, and the
    sweep's verdict takes the worse phase.
    """
    monkeypatch.setattr(limits, "DEFAULT_TIMEOUT_SECONDS", _BUDGET)
    trials = [_phase_trial(Phase.COLD, _BUDGET + 1)] + [
        _phase_trial(Phase.WARM, 1.0)
    ] * 30
    summary = build_report(run_metadata(), _HOST, trials).per_sweep[Sweep.COLD_WARM]
    assert summary.latency.p95_s == 1.0
    assert summary.per_phase[Phase.COLD].latency.p95_s == _BUDGET + 1
    assert summary.per_phase[Phase.COLD].verdict is Verdict.EXCEEDS
    assert summary.per_phase[Phase.WARM].verdict is Verdict.FITS
    assert set(summary.per_phase) == {Phase.COLD, Phase.WARM}
    assert summary.verdict is Verdict.EXCEEDS
