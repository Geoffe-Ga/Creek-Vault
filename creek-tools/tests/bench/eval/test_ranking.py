"""The ranked artifact: one row per model, four scores, a verdict, a caveat.

Rows are built from real :func:`run_eval` results over hand-written candidates,
so the ranking is tested against the harness's own scores rather than numbers
invented for the test.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.ranking import (
    NON_DISCRIMINATING,
    EvalMode,
    EvalRanking,
    ModelRow,
    rank_rows,
    write_ranking,
)
from creek_mcp.bench.eval.rubric import Dimension, JudgeKind, RubricVerdict
from creek_mcp.bench.eval.runner import EvalPlan, run_eval
from tests.bench.eval.conftest import (
    RecordingJudge,
    in_voice_candidate,
    slop_candidate,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.bench.eval.blinding import Candidate


def _row(tmp_path: Path, candidate: Candidate, invented: int = 0) -> ModelRow:
    """Score *candidate* alone, voice judged at a fixed 0.9, as a ranking row."""
    result = run_eval(
        EvalPlan(
            candidates=(candidate,),
            judge=RecordingJudge(score=0.9),
            seed=17,
            voice_evidence_present=True,
        ),
        tmp_path / candidate.model_tag,
    )
    return ModelRow(
        model_tag=candidate.model_tag,
        quality=result.quality,
        record=result.record,
        invented_citations=invented,
    )


def _ranking(rows: tuple[ModelRow, ...], judge: JudgeKind) -> EvalRanking:
    """Wrap *rows* in a ranking artifact."""
    return EvalRanking(
        mode=EvalMode.FAKE,
        judge=judge,
        seed=17,
        corpus_entries=16,
        generation_prompt="the prompt",
        rows=rank_rows(rows),
    )


def test_a_passing_model_outranks_a_failing_one(tmp_path: Path) -> None:
    """Verdict leads the order, whatever order the rows arrived in."""
    good = _row(tmp_path, in_voice_candidate("glm-5.3-flash"))
    bad = _row(tmp_path, slop_candidate("gpt-oss-120b"), invented=1)
    assert good.quality.verdict is RubricVerdict.PASSES
    assert bad.quality.verdict is RubricVerdict.FAILS
    assert [row.model_tag for row in rank_rows((bad, good))] == [
        "glm-5.3-flash",
        "gpt-oss-120b",
    ]


def test_identical_scores_rank_by_model_tag(tmp_path: Path) -> None:
    """A tie is broken by name, so the order never depends on input order."""
    one = _row(tmp_path, in_voice_candidate("glm-5.3"))
    two = _row(tmp_path, in_voice_candidate("gpt-oss-120b"))
    assert [row.model_tag for row in rank_rows((two, one))] == [
        "glm-5.3",
        "gpt-oss-120b",
    ]


def test_each_row_carries_four_scores_and_the_verdict(tmp_path: Path) -> None:
    """One row per model, every dimension present, verdict alongside (AC4)."""
    rows = (
        _row(tmp_path, in_voice_candidate("glm-5.3")),
        _row(tmp_path, slop_candidate("gpt-oss-120b"), invented=1),
    )
    payload = _ranking(rows, JudgeKind.HERMETIC).to_json_dict()
    assert payload["rubric_version"] == RUBRIC_VERSION
    assert [row["rank"] for row in payload["rows"]] == [1, 2]
    for row in payload["rows"]:
        assert set(row["dimensions"]) == {dimension.value for dimension in Dimension}
        assert row["verdict"] in {verdict.value for verdict in RubricVerdict}
        assert row["record"]["requests"]
    assert payload["rows"][1]["invented_citations"] == 1


def test_the_refusal_caveat_is_a_field_not_a_comment(tmp_path: Path) -> None:
    """The artifact itself says refusal does not discriminate (AC4)."""
    payload = _ranking(
        (_row(tmp_path, in_voice_candidate()),), JudgeKind.HERMETIC
    ).to_json_dict()
    caveats = payload["non_discriminating_dimensions"]
    assert caveats == [caveat.to_json_dict() for caveat in NON_DISCRIMINATING]
    (refusal,) = caveats
    assert refusal["dimension"] == Dimension.REFUSAL_BEHAVIOUR.value
    assert refusal["discriminates_between_models"] is False
    assert refusal["source"] == "pipeline_guards"
    assert payload["ranking_basis"]


def test_a_routed_judge_is_recorded_as_routed(tmp_path: Path) -> None:
    """A cloud judge never reads as a hermetic run in the ranking."""
    out = tmp_path / "ranking.json"
    write_ranking(
        _ranking((_row(tmp_path, in_voice_candidate()),), JudgeKind.ROUTED), out
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["judge"] == "routed"
    assert payload["mode"] == "fake"
    assert payload["generation_prompt"] == "the prompt"
