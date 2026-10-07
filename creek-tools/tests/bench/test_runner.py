"""The trial runner: the real reflect path, each sweep, and its refusals.

Every trial goes through :func:`creek_mcp.tools.reflect.reflect_tool` with the
production care guard, so what is timed is a real reflection — grounding,
prompt build, model call, parse — against the synthetic corpus. These tests
pin each sweep's shape (cold trials evict first, concurrency is genuinely
simultaneous, idle cycles really wait) and that every refusal happens before
the harness writes a byte or sends a request.
"""

from __future__ import annotations

import errno
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from creek.classify.llm.router import IntimateRoutingError
from creek.config import AuthorConfig
from creek_mcp.bench.deadline import Verdict
from creek_mcp.bench.local_only import CloudProviderRefusedError, LocalOnlyFactory
from creek_mcp.bench.ollama_client import PinnedModel
from creek_mcp.bench.outcome import (
    ContextOverflowError,
    Outcome,
    ProviderUnavailableError,
)
from creek_mcp.bench.runner import (
    PROMPT_OVERHEAD_TOKENS,
    WorkloadError,
    max_context_words,
    run_bench,
)
from creek_mcp.bench.trial import Phase, Sweep
from creek_mcp.tools.reflect import _build_prompt
from tests.bench.conftest import FakeModel, Recorded, make_plan, run_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.bench.metadata import RunMetadata
    from creek_mcp.bench.report import BenchReport
    from creek_mcp.bench.trial import Trial

_DIGEST = "sha256:" + "e" * 64
_SHA = "f" * 40
_REFLECT_MAX_OUTPUT_TOKENS = 128
"""The hard output cap reflect itself applies (#1820)."""


def _trials(report: BenchReport, sweep: Sweep) -> tuple[Trial, ...]:
    """Return the trials *report* recorded for *sweep*."""
    return report.per_sweep[sweep].trials


def _only_warm(**workload: Any) -> dict[str, Any]:
    """Workload overrides for a single warm trial and nothing else."""
    base: dict[str, Any] = {
        "cold_trials": 0,
        "warm_trials": 1,
        "concurrency_levels": (),
    }
    base.update(workload)
    return base


def test_fake_run_all_ok_on_synthetic_corpus(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """Every trial on the synthetic corpus is an ``ok`` reflection."""
    report = run_bench(make_plan(fake_model), tmp_path / "corpus")
    trials = [t for summary in report.per_sweep.values() for t in summary.trials]
    assert len(trials) == 4
    assert {t.outcome for t in trials} == {Outcome.OK}
    assert fake_model.calls == 4
    assert report.verdict is Verdict.FITS


def test_trial_records_input_size_generation_share_and_residency(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """Each trial knows its input size, model share, and resident size."""
    plan = make_plan(fake_model, query_words=13)
    plan = replace(plan, backend=replace(plan.backend, resident_bytes=lambda: 123))
    report = run_bench(plan, tmp_path / "c")
    for trial in _trials(report, Sweep.COLD_WARM):
        assert trial.input_words == 13
        assert trial.generation_s is not None
        assert trial.generation_s <= trial.latency_s
        assert trial.model_resident_bytes == 123


@pytest.mark.parametrize("site", ["callable", "factory"])
def test_probe_sees_exception_reflect_swallows(tmp_path: Path, site: str) -> None:
    """Reflect turns a RuntimeError into a refusal; the probe still classifies it."""
    down = ProviderUnavailableError("down")
    model = (
        FakeModel(call_exc=down)
        if site == "callable"
        else FakeModel(factory_exc=down, factory_fails_after=1)
    )
    report = run_bench(make_plan(model, **_only_warm()), tmp_path / "corpus")
    (trial,) = _trials(report, Sweep.COLD_WARM)
    assert trial.outcome is Outcome.PROVIDER_UNAVAILABLE
    assert report.verdict is Verdict.EXCEEDS


def test_intimate_routing_refusal_is_an_error_not_unavailability(
    tmp_path: Path,
) -> None:
    """A privacy refusal is not a capacity signal and must not read as one."""
    model = FakeModel(
        factory_exc=IntimateRoutingError("no local"), factory_fails_after=1
    )
    report = run_bench(make_plan(model, **_only_warm()), tmp_path / "corpus")
    (trial,) = _trials(report, Sweep.COLD_WARM)
    assert trial.outcome is Outcome.ERROR


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (MemoryError(), Outcome.OOM),
        (OSError(errno.ENOSPC, "full"), Outcome.DISK_FULL),
        (TimeoutError(), Outcome.TIMEOUT),
        (
            httpx.ReadTimeout("slow", request=httpx.Request("POST", "http://x")),
            Outcome.TIMEOUT,
        ),
    ],
)
def test_memoryerror_and_enospc_propagate_and_classify(
    tmp_path: Path, exc: BaseException, expected: Outcome
) -> None:
    """Failures reflect does not swallow are caught per trial and classified."""
    model = FakeModel(call_exc=exc, raise_first_only=True)
    report = run_bench(make_plan(model, **_only_warm(warm_trials=2)), tmp_path / "c")
    outcomes = [t.outcome for t in _trials(report, Sweep.COLD_WARM)]
    assert outcomes == [expected, Outcome.OK]
    assert report.verdict is Verdict.EXCEEDS


def test_unexpected_exception_type_is_not_swallowed(tmp_path: Path) -> None:
    """A harness bug (a type no trial can raise) fails the run loudly."""
    model = FakeModel(call_exc=KeyError("bug"))
    with pytest.raises(KeyError):
        run_bench(make_plan(model, **_only_warm()), tmp_path / "corpus")


def test_refusal_without_a_probe_outcome_is_an_error(tmp_path: Path) -> None:
    """A non-ok reflect status the probe did not see still counts as failed."""
    model = FakeModel()
    report = run_bench(make_plan(model, **_only_warm(query_words=0)), tmp_path / "c")
    (trial,) = _trials(report, Sweep.COLD_WARM)
    assert trial.outcome is Outcome.ERROR
    assert model.calls == 0


@pytest.mark.parametrize("is_cloud", [True, None])
def test_refuses_cloud_provider(tmp_path: Path, is_cloud: bool | None) -> None:
    """A cloud or undeclared callable is refused before any write or call."""
    model = FakeModel(is_cloud=is_cloud)
    root = tmp_path / "corpus"
    with pytest.raises(CloudProviderRefusedError):
        run_bench(make_plan(model), root)
    assert model.calls == 0
    assert not root.exists()


def test_refuses_cloud_provider_name(fake_model: FakeModel) -> None:
    """A cloud provider name never even becomes a factory."""
    with pytest.raises(CloudProviderRefusedError):
        LocalOnlyFactory(fake_model.factory, provider_name="anthropic")
    assert fake_model.builds == 0


def test_cold_trials_evict_first(tmp_path: Path, fake_model: FakeModel) -> None:
    """Each cold trial is preceded by an eviction; warm trials are not."""
    recorded = Recorded()
    plan = make_plan(
        fake_model, recorded, cold_trials=2, warm_trials=2, concurrency_levels=()
    )
    report = run_bench(plan, tmp_path / "corpus")
    assert recorded.log == ["evict", "trial", "evict", "trial", "trial", "trial"]
    phases = [t.phase for t in _trials(report, Sweep.COLD_WARM)]
    assert phases == [Phase.COLD, Phase.COLD, Phase.WARM, Phase.WARM]


def test_context_sweep_runs_each_size(tmp_path: Path, fake_model: FakeModel) -> None:
    """The context sweep reflects one entry of each requested size."""
    plan = make_plan(fake_model, **_only_warm(warm_trials=0, context_sizes=(16, 64)))
    report = run_bench(plan, tmp_path / "corpus")
    trials = _trials(report, Sweep.CONTEXT)
    assert [t.input_words for t in trials] == [16, 64]
    assert {t.outcome for t in trials} == {Outcome.OK}


def test_context_sweep_refuses_above_num_ctx(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """A size beyond the pinned context window is refused before any write."""
    root = tmp_path / "corpus"
    plan = make_plan(fake_model, context_sizes=(4097,))
    with pytest.raises(WorkloadError, match="num_ctx"):
        run_bench(plan, root)
    assert not root.exists()
    assert fake_model.builds == 0


@pytest.mark.parametrize("level", [1, 2, 4])
def test_concurrency_sweep_runs_level_simultaneous(tmp_path: Path, level: int) -> None:
    """Exactly *level* reflections are in flight together, never serialised."""
    model = FakeModel(gather=level)
    plan = make_plan(model, cold_trials=0, warm_trials=0, concurrency_levels=(level,))
    report = run_bench(plan, tmp_path / "corpus")
    trials = _trials(report, Sweep.CONCURRENCY)
    assert model.max_in_flight == level
    assert len(trials) == level
    assert {t.concurrency for t in trials} == {level}


def test_concurrency_sweep_trial_count_is_sum_of_levels(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """Levels 1, 2 and 4 give seven trials, tagged with their level."""
    plan = make_plan(
        fake_model, cold_trials=0, warm_trials=0, concurrency_levels=(1, 2, 4)
    )
    trials = _trials(run_bench(plan, tmp_path / "corpus"), Sweep.CONCURRENCY)
    assert sorted(t.concurrency for t in trials) == [1, 2, 2, 4, 4, 4, 4]


def test_idle_cycle_evicts_sleeps_resumes(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """Each idle cycle evicts, waits the idle period, then resumes."""
    recorded = Recorded()
    plan = make_plan(
        fake_model,
        recorded,
        **_only_warm(warm_trials=0, idle_cycles=2, idle_seconds=5.0),
    )
    report = run_bench(plan, tmp_path / "corpus")
    assert recorded.slept == [5.0, 5.0]
    assert recorded.log == ["evict", "sleep", "trial", "evict", "sleep", "trial"]
    assert {t.phase for t in _trials(report, Sweep.IDLE)} == {Phase.RESUME}


def test_factory_gets_reflect_budget(tmp_path: Path, fake_model: FakeModel) -> None:
    """Trials request reflect's own bounded output budget, never more."""
    run_bench(make_plan(fake_model), tmp_path / "corpus")
    trial_budgets = fake_model.max_tokens_seen[1:]
    assert trial_budgets
    assert all(0 < budget <= _REFLECT_MAX_OUTPUT_TOKENS for budget in trial_budgets)


def test_no_grounding_mode_sends_no_sources(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """The hermetic mode grounds on nothing, and the prompt says so."""
    run_bench(make_plan(fake_model, **_only_warm()), tmp_path / "corpus")
    assert "SOURCE FRAGMENTS:\n(none)" in fake_model.prompts[0]


def test_default_grounding_path_runs_hermetically(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """The production grounder runs (on the suite's mocked encoder) and grounds."""
    plan = make_plan(
        fake_model,
        metadata=run_metadata(grounding="default"),
        cold_trials=1,
        warm_trials=1,
        concurrency_levels=(2,),
    )
    report = run_bench(plan, tmp_path / "corpus")
    assert {t.outcome for s in report.per_sweep.values() for t in s.trials} == {
        Outcome.OK
    }
    assert all("- bench note 000" in prompt for prompt in fake_model.prompts)


def test_audit_rows_land_in_the_corpus_only(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """Reflect's audit append goes to the synthetic corpus, once per trial."""
    root = tmp_path / "corpus"
    run_bench(make_plan(fake_model), root)
    audit = root / "00-Creek-Meta" / "audit" / "mcp.jsonl"
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 4


def _live_metadata() -> RunMetadata:
    """Live-mode metadata with a digest and git sha."""
    return run_metadata(
        mode="live",
        provider="ollama",
        grounding="none",
        digest=_DIGEST,
        git_sha=_SHA,
    )


def test_live_mode_pins_before_writing_and_records_details(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """A live run verifies the digest first, then records the weights' details."""
    root = tmp_path / "corpus"
    pinned_with: list[str] = []

    def _pin(expected: str) -> PinnedModel:
        assert not root.exists(), "the digest must be verified before any write"
        pinned_with.append(expected)
        return PinnedModel(
            digest=expected,
            quantization="Q4_0",
            parameter_count="7.2B",
            license_id="Apache-2.0",
        )

    plan = make_plan(fake_model, metadata=_live_metadata(), **_only_warm())
    plan = replace(plan, backend=replace(plan.backend, pin=_pin))
    report = run_bench(plan, root)
    assert pinned_with == [_DIGEST]
    assert report.run.digest == _DIGEST
    assert report.run.quantization == "Q4_0"
    assert report.run.parameter_count == "7.2B"
    assert report.run.license_id == "Apache-2.0"


def test_live_mode_without_pin_hook_refused(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """A live plan that cannot verify its digest is refused."""
    plan = make_plan(fake_model, metadata=_live_metadata(), **_only_warm())
    with pytest.raises(WorkloadError, match="digest"):
        run_bench(plan, tmp_path / "corpus")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"concurrency_levels": (0,)}, "concurrency"),
        ({"cold_trials": -1}, "trial counts"),
        ({"idle_cycles": -1}, "trial counts"),
        ({"words_per_entry": -1}, "trial counts"),
        ({"idle_seconds": -0.5}, "idle_seconds"),
        ({"context_sizes": (0,)}, "num_ctx"),
    ],
)
def test_workload_validation(
    tmp_path: Path,
    fake_model: FakeModel,
    overrides: dict[str, Any],
    message: str,
) -> None:
    """A malformed workload is refused before anything runs."""
    root = tmp_path / "corpus"
    with pytest.raises(WorkloadError, match=message):
        run_bench(make_plan(fake_model, **overrides), root)
    assert not root.exists()


def _runtime_down() -> None:
    """A backend side call against an Ollama that has gone away."""
    raise ProviderUnavailableError("down")


def _residency_down() -> int | None:
    """``/api/ps`` against an Ollama that has gone away."""
    raise ProviderUnavailableError("down")


def test_dropped_runtime_is_recorded_per_trial_not_fatal(tmp_path: Path) -> None:
    """A runtime that drops mid-run yields classified trials and a report.

    Evict before each cold or idle trial and the residency probe after every
    trial both fail. The run must still finish: the evict failure is that
    trial's ``provider_unavailable`` outcome, and residency is simply unknown.
    """
    plan = make_plan(
        FakeModel(),
        cold_trials=2,
        warm_trials=2,
        concurrency_levels=(2,),
        idle_cycles=1,
    )
    plan = replace(
        plan,
        backend=replace(
            plan.backend, evict=_runtime_down, resident_bytes=_residency_down
        ),
    )
    report = run_bench(plan, tmp_path / "corpus")
    cold_warm = _trials(report, Sweep.COLD_WARM)
    cold = [t.outcome for t in cold_warm if t.phase is Phase.COLD]
    warm = [t.outcome for t in cold_warm if t.phase is Phase.WARM]
    assert cold == [Outcome.PROVIDER_UNAVAILABLE] * 2
    assert warm == [Outcome.OK] * 2
    assert [t.outcome for t in _trials(report, Sweep.IDLE)] == [
        Outcome.PROVIDER_UNAVAILABLE
    ]
    assert {t.outcome for t in _trials(report, Sweep.CONCURRENCY)} == {Outcome.OK}
    assert all(
        t.model_resident_bytes is None
        for s in report.per_sweep.values()
        for t in s.trials
    )
    assert report.verdict is Verdict.EXCEEDS


def test_dropped_runtime_with_failing_model_still_reports(tmp_path: Path) -> None:
    """With the model also unreachable, every trial is classified, none raised."""
    model = FakeModel(call_exc=ProviderUnavailableError("down"))
    plan = make_plan(model, cold_trials=1, warm_trials=1, concurrency_levels=(2,))
    plan = replace(
        plan,
        backend=replace(
            plan.backend, evict=_runtime_down, resident_bytes=_residency_down
        ),
    )
    report = run_bench(plan, tmp_path / "corpus")
    outcomes = {t.outcome for s in report.per_sweep.values() for t in s.trials}
    assert outcomes == {Outcome.PROVIDER_UNAVAILABLE}
    assert report.verdict is Verdict.EXCEEDS


def test_context_sweep_upper_boundary_is_inclusive_and_conservative(
    tmp_path: Path, fake_model: FakeModel
) -> None:
    """The largest admitted entry leaves room for the template and the output.

    ``num_ctx`` minus the prompt-overhead allowance minus ``num_predict`` is
    admitted (the inclusive edge AC8 promises); one word more is refused.
    """
    metadata = run_metadata(num_ctx=2048, num_predict=64)
    limit = max_context_words(metadata)
    assert limit == 2048 - PROMPT_OVERHEAD_TOKENS - 64
    plan = make_plan(
        fake_model,
        metadata=metadata,
        **_only_warm(warm_trials=0, context_sizes=(limit,)),
    )
    (trial,) = _trials(run_bench(plan, tmp_path / "ok"), Sweep.CONTEXT)
    assert trial.input_words == limit
    over = make_plan(
        fake_model,
        metadata=metadata,
        **_only_warm(warm_trials=0, context_sizes=(limit + 1,)),
    )
    with pytest.raises(WorkloadError, match="num_ctx"):
        run_bench(over, tmp_path / "over")
    assert not (tmp_path / "over").exists()


def test_prompt_overhead_bounds_the_real_reflect_template() -> None:
    """The allowance covers reflect's template at its largest, in bytes.

    A byte-level tokenizer spends at least one byte per token, so the byte
    length of the template (with every grounding slot filled and the largest
    note budget) is an upper bound on its tokens. If reflect's prompt grows
    past the allowance, this fails rather than the sweep silently truncating.
    """
    top_k = AuthorConfig().retrieval_top_k
    template = _build_prompt("", ["bench note 0000"] * top_k, max_notes=10)
    assert len(template.encode("utf-8")) <= PROMPT_OVERHEAD_TOKENS


def test_truncated_prompt_is_a_context_overflow_not_ok(tmp_path: Path) -> None:
    """A runtime-reported truncation survives reflect's refusal and is non-ok."""
    model = FakeModel(call_exc=ContextOverflowError("full"))
    plan = make_plan(model, **_only_warm(warm_trials=0, context_sizes=(32,)))
    report = run_bench(plan, tmp_path / "corpus")
    (trial,) = _trials(report, Sweep.CONTEXT)
    assert trial.outcome is Outcome.CONTEXT_OVERFLOW
    assert report.per_sweep[Sweep.CONTEXT].verdict is Verdict.EXCEEDS
