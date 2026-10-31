"""``bench eval``: generate a candidate per model, score each, rank them.

The default lane is hermetic by construction and by assertion: every test that
runs the command poisons sockets and the provider factory, so a path that
reached a provider would fail loudly instead of billing quietly. ``--mode
live`` is exercised only for its refusals, plus once through an injected fake
provider factory and an injected environment mapping — no real key, consent
variable or endpoint is ever set.
"""

from __future__ import annotations

import argparse
import json
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.completion import Completion
from creek_mcp.bench import cli
from creek_mcp.bench.eval import command
from creek_mcp.bench.eval.candidates import FakeCandidateSource
from creek_mcp.bench.eval.command import (
    MAX_MODELS,
    RankedEvalPlan,
    add_eval_arguments,
    plan_from_args,
    run_ranked_eval,
)
from creek_mcp.bench.eval.judge import HermeticJudge, RoutedJudge
from creek_mcp.bench.eval.ranking import EvalMode
from creek_mcp.bench.eval.rubric import JudgeKind
from creek_mcp.bench.eval.runner import EvalPlanError
from tests.bench.eval.conftest import RecordingJudge

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import LLMConfig

_MODELS = "gpt-oss-120b,glm-5.3,glm-5.3-flash"
"""Example chat models from the owner's catalog. Named, never dialled."""

_KEY_ENV = "CREEK_BENCH_EVAL_TEST_KEY_1989"
"""A variable name no test ever sets in the real environment."""


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens a socket or builds a real provider."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the hermetic eval reached for the network")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(command, "build_provider", _refuse)
    monkeypatch.delenv(_KEY_ENV, raising=False)


@dataclass
class StubProvider:
    """A provider double: canned in-voice text, calls recorded, no socket."""

    model: str
    available: bool = True
    is_cloud: bool = True
    calls: list[int | None] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Answer with the fake source's text for this model."""
        del system
        self.calls.append(max_tokens)
        return Completion(text=FakeCandidateSource(self.model).generate(prompt))


def _eval(out: Path, *extra: str) -> list[str]:
    """Return argv for a hermetic ``eval`` run writing *out*."""
    return ["eval", "--models", _MODELS, "--seed", "17", "--out", str(out), *extra]


def _args(*extra: str) -> argparse.Namespace:
    """Parse ``eval`` flags with the same registration :func:`cli.main` uses."""
    parser = argparse.ArgumentParser()
    add_eval_arguments(parser)
    return parser.parse_args(["--models", _MODELS, "--out", "unused.json", *extra])


@pytest.mark.usefixtures("sealed")
def test_the_default_run_is_hermetic_and_ranks_every_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """fake + hermetic is the default and needs no network, key or spend (AC3)."""
    out = tmp_path / "eval.json"
    assert cli.main(_eval(out)) == cli.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "fake"
    assert payload["judge"] == "hermetic"
    assert payload["seed"] == 17
    assert sorted(row["model_tag"] for row in payload["rows"]) == sorted(
        _MODELS.split(",")
    )
    assert str(out) in capsys.readouterr().out


@pytest.mark.usefixtures("sealed")
def test_two_runs_at_one_seed_are_byte_identical(tmp_path: Path) -> None:
    """The reproducibility run_eval claims holds for the whole artifact (AC3)."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert cli.main(_eval(first)) == cli.EXIT_OK
    assert cli.main(_eval(second)) == cli.EXIT_OK
    assert first.read_bytes() == second.read_bytes()


def test_the_defaults_are_the_hermetic_pair() -> None:
    """Nothing but an explicit flag selects a provider-reaching mode (AC5)."""
    args = _args()
    assert (args.mode, args.judge) == ("fake", "hermetic")


@pytest.mark.usefixtures("sealed")
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(("--mode", "live"), id="live-without-provider"),
        pytest.param(("--mode", "live", "--provider", "openai"), id="live-no-key"),
        pytest.param(
            ("--mode", "live", "--provider", "openai", "--api-key-env", _KEY_ENV),
            id="live-key-unset",
        ),
        pytest.param(("--provider", "openai"), id="fake-with-live-flag"),
        pytest.param(("--judge-provider", "openai"), id="hermetic-with-judge-flag"),
        pytest.param(("--judge", "routed"), id="routed-without-provider"),
        pytest.param(
            ("--judge", "routed", "--judge-provider", "openai"),
            id="routed-no-key",
        ),
        pytest.param(("--max-tokens", "0"), id="no-token-budget"),
        pytest.param(("--max-tokens", "100000"), id="unbounded-token-budget"),
    ],
)
def test_a_provider_reaching_run_without_credentials_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    """Every refusal happens before a provider is built or a byte is sent (AC5)."""
    out = tmp_path / "eval.json"
    assert cli.main(_eval(out, *extra)) == cli.EXIT_REFUSED
    assert not out.exists()
    assert capsys.readouterr().err.startswith("refused: ")


@pytest.mark.usefixtures("sealed")
@pytest.mark.parametrize(
    "models",
    [
        pytest.param(",", id="empty"),
        pytest.param("glm-5.3,glm-5.3", id="duplicate"),
        pytest.param(",".join(f"m{i}" for i in range(MAX_MODELS + 1)), id="too-many"),
    ],
)
def test_a_model_list_outside_the_bound_is_refused(tmp_path: Path, models: str) -> None:
    """A run is bounded: a handful of distinct models, never an open sweep."""
    argv = ["eval", "--models", models, "--out", str(tmp_path / "e.json")]
    assert cli.main(argv) == cli.EXIT_REFUSED


@pytest.mark.usefixtures("sealed")
def test_a_routed_judge_is_recorded_as_routed(tmp_path: Path) -> None:
    """A cloud judge's run says ``judge: routed`` in the artifact (AC5)."""
    plan = RankedEvalPlan(
        sources={"glm-5.3": FakeCandidateSource("glm-5.3")},
        judge=RecordingJudge(score=0.9, is_cloud=True),
        judge_kind=JudgeKind.ROUTED,
    )
    ranking = run_ranked_eval(plan, tmp_path / "scratch")
    assert ranking.to_json_dict()["judge"] == "routed"
    assert ranking.rows[0].quality.judge is JudgeKind.ROUTED


@pytest.mark.usefixtures("sealed")
def test_a_cloud_judge_cannot_claim_a_hermetic_run(tmp_path: Path) -> None:
    """run_eval's refusal still governs: the declaration must match the judge."""
    plan = RankedEvalPlan(
        sources={"glm-5.3": FakeCandidateSource("glm-5.3")},
        judge=RecordingJudge(is_cloud=True),
    )
    with pytest.raises(EvalPlanError):
        run_ranked_eval(plan, tmp_path / "scratch")


@pytest.mark.usefixtures("sealed")
def test_a_model_that_invents_a_citation_is_counted(tmp_path: Path) -> None:
    """The row records invented citations, so a reader sees why grounding fell."""

    @dataclass
    class _Inventing:
        def generate(self, prompt: str) -> str:
            return FakeCandidateSource("x").generate(prompt) + " [[voice-9999]]"

    plan = RankedEvalPlan(sources={"glm-5.3": _Inventing()}, judge=HermeticJudge())
    (row,) = run_ranked_eval(plan, tmp_path / "scratch").rows
    assert row.invented_citations == 1


def test_a_live_plan_builds_one_bounded_source_per_model() -> None:
    """Live wiring, through an injected factory and environment only.

    No real key, consent variable or endpoint is set: the environment is a
    mapping handed to the function, and the factory returns a stub.
    """
    built: list[LLMConfig] = []
    stubs: dict[str, StubProvider] = {}

    def _factory(config: LLMConfig) -> StubProvider:
        built.append(config)
        return stubs.setdefault(config.model or "", StubProvider(config.model or ""))

    args = _args(
        "--mode",
        "live",
        "--provider",
        "openai",
        "--api-base",
        "http://127.0.0.1:9/v1",
        "--api-key-env",
        _KEY_ENV,
        "--max-tokens",
        "300",
        "--judge",
        "routed",
        "--judge-provider",
        "anthropic",
        "--judge-model",
        "judge-model",
        "--judge-api-key-env",
        _KEY_ENV,
    )
    plan = plan_from_args(args, environ={_KEY_ENV: "x"}, provider_factory=_factory)
    assert plan.mode is EvalMode.LIVE
    assert plan.judge_kind is JudgeKind.ROUTED
    assert isinstance(plan.judge, RoutedJudge)
    assert list(plan.sources) == _MODELS.split(",")
    assert [config.model for config in built] == _MODELS.split(",")
    assert {config.api_base for config in built} == {"http://127.0.0.1:9/v1"}
    assert {config.api_key_env for config in built} == {_KEY_ENV}
    plan.sources["glm-5.3"].generate("[voice-0000] I went out.")
    assert stubs["glm-5.3"].calls == [300]
