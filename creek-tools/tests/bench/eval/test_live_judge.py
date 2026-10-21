"""Live frontier-judge smoke, opt-in and never run in CI (Decision 4).

The owner has authorized **no spend yet**: design and measure only. So this is
the only path in the tree that can reach a real judge, it is behind the
``live`` marker that CI never selects, and it skips itself unless the operator
has set both ``CREEK_EVAL_JUDGE_PROVIDER`` and ``CREEK_EVAL_JUDGE_MODEL``. The
text it sends is the synthetic corpus and a synthetic candidate — the eval has
no other kind.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek_mcp.bench.eval.judge import RoutedJudge
from creek_mcp.bench.eval.rubric import Dimension, JudgeKind
from creek_mcp.bench.eval.runner import EvalPlan, run_eval
from tests.bench.eval.conftest import in_voice_candidate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.live, pytest.mark.slow]

_PROVIDER_ENV = "CREEK_EVAL_JUDGE_PROVIDER"
_MODEL_ENV = "CREEK_EVAL_JUDGE_MODEL"


@pytest.fixture
def routed_judge() -> RoutedJudge:
    """A judge routed to the operator-named provider, or skip the test."""
    provider = os.environ.get(_PROVIDER_ENV)
    model = os.environ.get(_MODEL_ENV)
    if not provider or not model:
        pytest.skip(f"set {_PROVIDER_ENV} and {_MODEL_ENV} to run the live judge")
    router = ModelRouter(
        LLMRoutingConfig(
            default=LLMConfig(provider="ollama"),
            writing_desk={"voice_judge": LLMConfig(provider=provider, model=model)},
        )
    )
    return RoutedJudge(router)


def test_the_routed_judge_scores_a_synthetic_candidate(
    routed_judge: RoutedJudge, tmp_path: Path
) -> None:
    """One real judge call over synthetic text only, on the operator's say-so."""
    plan = EvalPlan(
        candidates=(in_voice_candidate(),),
        judge=routed_judge,
        judge_kind=JudgeKind.ROUTED,
        seed=3,
        voice_evidence_present=True,
    )
    result = run_eval(plan, tmp_path / "corpus")
    voice = result.quality.per_dimension[Dimension.VOICE_MATCH]
    assert 0.0 <= voice.score <= 1.0
