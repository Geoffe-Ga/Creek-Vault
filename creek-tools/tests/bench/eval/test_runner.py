"""End-to-end eval runs: hermetic, reproducible, confined, and content-safe.

A run scores one or more candidates on all four dimensions against the
synthetic corpus with the hermetic judge, makes no network call, and writes a
reproducible artifact naming its seed and rubric version. The prompts #1875
asks to be recorded live in *that* artifact, never in the benchmark report.
"""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from creek_mcp.bench.corpus import CorpusConfinementError
from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.judge import HermeticJudge
from creek_mcp.bench.eval.record import write_record
from creek_mcp.bench.eval.rubric import (
    Basis,
    Dimension,
    JudgeKind,
    RubricVerdict,
)
from creek_mcp.bench.eval.runner import EvalPlan, EvalPlanError, run_eval
from tests.bench.eval.conftest import (
    RecordingJudge,
    in_voice_candidate,
    slop_candidate,
)

_BASE_PLAN = EvalPlan(
    candidates=(in_voice_candidate(),),
    judge=HermeticJudge(),
    seed=17,
    voice_evidence_present=True,
)
"""One in-voice candidate, hermetic judge, voice evidence declared present."""


def _plan(**overrides: object) -> EvalPlan:
    """Build a one-candidate hermetic plan, overridden by *overrides*."""
    return replace(_BASE_PLAN, **overrides)


def test_a_run_scores_all_four_dimensions(tmp_path: Path) -> None:
    """Every dimension in #1875 is reported separately (AC1)."""
    result = run_eval(_plan(), tmp_path / "corpus")
    assert set(result.quality.per_dimension) == set(Dimension)
    assert result.quality.rubric_version == RUBRIC_VERSION
    assert result.quality.seed == 17
    assert result.quality.judge is JudgeKind.HERMETIC
    assert result.quality.candidates == 1


def test_a_run_writes_a_reproducible_artifact(tmp_path: Path) -> None:
    """The artifact names its seed and rubric version (AC1)."""
    result = run_eval(_plan(), tmp_path / "corpus")
    out = tmp_path / "eval.json"
    write_record(result.record, out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["seed"] == 17
    assert payload["rubric_version"] == RUBRIC_VERSION
    assert payload["judge"] == JudgeKind.HERMETIC.value
    assert payload["requests"], "the prompts #1875 asks for are recorded"


def test_two_runs_at_one_seed_produce_identical_scores(tmp_path: Path) -> None:
    """Determinism is the precondition for comparing two models (AC2)."""
    first = run_eval(_plan(), tmp_path / "a")
    second = run_eval(_plan(), tmp_path / "b")
    assert first.quality == second.quality


def test_a_different_seed_does_not_move_a_fixed_output_s_score(
    tmp_path: Path,
) -> None:
    """Presentation order is reseeded; the score of a fixed output is not (AC2)."""
    candidates = (in_voice_candidate("alpha"), slop_candidate("beta"))
    low = run_eval(_plan(candidates=candidates, seed=1), tmp_path / "low")
    high = run_eval(_plan(candidates=candidates, seed=99), tmp_path / "high")
    assert {d: s.score for d, s in low.quality.per_dimension.items()} == {
        d: s.score for d, s in high.quality.per_dimension.items()
    }
    low_ids = [request.candidate_id for request in low.record.requests]
    high_ids = [request.candidate_id for request in high.record.requests]
    assert low_ids != high_ids, "the opaque labels are reseeded"


def test_the_judge_never_receives_the_candidate_model_identity(
    tmp_path: Path,
) -> None:
    """Asserted on the exact payloads handed to the judge (AC3)."""
    judge = RecordingJudge()
    candidates = (in_voice_candidate("alpha-tiny"), slop_candidate("beta-frontier"))
    run_eval(_plan(candidates=candidates, judge=judge), tmp_path / "corpus")
    assert judge.requests
    for request in judge.requests:
        blob = json.dumps(
            {
                "candidate_id": request.candidate_id,
                "dimension": request.dimension.value,
                "rubric_version": request.rubric_version,
                "candidate_text": request.candidate_text,
                "reference_text": request.reference_text,
            }
        )
        for candidate in candidates:
            assert candidate.model_tag not in blob


def test_an_invented_citation_scores_zero_on_grounding(tmp_path: Path) -> None:
    """A fragment the corpus does not contain earns nothing (AC5)."""
    candidate = in_voice_candidate(citations=("voice-9999",))
    result = run_eval(_plan(candidates=(candidate,)), tmp_path / "corpus")
    assert result.quality.per_dimension[Dimension.GROUNDING_ACCURACY].score == 0.0


def test_correct_refusals_score_full_marks(tmp_path: Path) -> None:
    """Care guard, above-ceiling prompt and INTIMATE fragment refused (AC6)."""
    result = run_eval(_plan(), tmp_path / "corpus")
    assert result.quality.per_dimension[Dimension.REFUSAL_BEHAVIOUR].score == 1.0


def test_an_answer_in_place_of_a_refusal_scores_zero(tmp_path: Path) -> None:
    """The other half of AC6: answering is a total failure on that dimension."""
    result = run_eval(_plan(candidates=(slop_candidate(),)), tmp_path / "corpus")
    assert result.quality.per_dimension[Dimension.REFUSAL_BEHAVIOUR].score == 0.0
    assert result.quality.verdict is RubricVerdict.FAILS


def test_voice_match_is_unsupported_by_default(tmp_path: Path) -> None:
    """#1880: today's prompt carries no voice evidence, so the default says so."""
    result = run_eval(_plan(voice_evidence_present=False), tmp_path / "corpus")
    voice = result.quality.per_dimension[Dimension.VOICE_MATCH]
    assert voice.basis is Basis.UNSUPPORTED
    assert result.quality.verdict is RubricVerdict.INSUFFICIENT_EVIDENCE


def test_a_run_refuses_a_corpus_root_outside_the_temp_dir() -> None:
    """The eval can never read or write a real vault (AC8)."""
    with pytest.raises(CorpusConfinementError):
        run_eval(_plan(), Path.home() / "Creek-Vault-not-a-temp-dir")


def test_a_run_refuses_an_empty_candidate_set(tmp_path: Path) -> None:
    """An eval of nothing must not report a verdict."""
    with pytest.raises(EvalPlanError, match="candidate"):
        run_eval(_plan(candidates=()), tmp_path / "corpus")


def test_the_record_carries_only_synthetic_text(tmp_path: Path) -> None:
    """Every prompt in the artifact is corpus or candidate text, nothing else."""
    candidate = in_voice_candidate()
    result = run_eval(_plan(candidates=(candidate,)), tmp_path / "corpus")
    allowed = {candidate.text, result.corpus.voice_sample}
    for request in result.record.requests:
        assert request.candidate_text in allowed
        assert request.reference_text in allowed


def test_the_record_never_names_a_candidate_model(tmp_path: Path) -> None:
    """The blinding survives serialisation of the prompt-bearing artifact."""
    candidates = (in_voice_candidate("alpha-tiny"), slop_candidate("beta-frontier"))
    result = run_eval(_plan(candidates=candidates), tmp_path / "corpus")
    out = tmp_path / "eval.json"
    write_record(result.record, out)
    written = out.read_text(encoding="utf-8")
    for candidate in candidates:
        assert candidate.model_tag not in written


def test_a_run_makes_no_outbound_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hermetic lane is hermetic: sockets are poisoned for the duration."""

    def _no_sockets(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the eval opened a socket")

    monkeypatch.setattr(socket.socket, "connect", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    result = run_eval(_plan(), tmp_path / "corpus")
    assert result.quality.candidates == 1


def test_a_cheaper_model_that_passes_stops_the_ladder(tmp_path: Path) -> None:
    """The owner's bar is not frontier: a pass means no escalation (#1849)."""
    judge = RecordingJudge(score=1.0)
    plan = _plan(judge=judge)
    result = run_eval(plan, tmp_path / "corpus")
    assert result.quality.verdict is RubricVerdict.PASSES
    assert result.next_model(("cheap", "mid", "frontier"), "cheap") is None


def test_a_failing_model_escalates_one_rung(tmp_path: Path) -> None:
    """Only a rubric failure buys the next model up."""
    result = run_eval(_plan(candidates=(slop_candidate(),)), tmp_path / "corpus")
    assert result.next_model(("cheap", "mid", "frontier"), "cheap") == "mid"


def test_the_plan_can_shrink_the_corpus(tmp_path: Path) -> None:
    """A smaller corpus is still in #1875's 10-20 range."""
    result = run_eval(_plan(entries=10), tmp_path / "corpus")
    assert result.corpus.entry_count == 10


def test_candidate_text_is_never_mutated(tmp_path: Path) -> None:
    """Scoring reads the candidate; it never edits what it scored."""
    candidate = in_voice_candidate()
    before = candidate.text
    run_eval(_plan(candidates=(candidate,)), tmp_path / "corpus")
    assert replace(candidate).text == before


def test_a_run_refuses_a_cloud_judge_declared_hermetic(tmp_path: Path) -> None:
    """An artifact claiming a hermetic run over a cloud judge would be a lie."""

    class _CloudJudge:
        """A judge that declares itself cloud."""

        is_cloud = True

        def __call__(self, request: object) -> float:
            """Never reached: the plan is refused first."""
            del request
            raise AssertionError("the judge was reached")

    with pytest.raises(EvalPlanError, match="judge_kind"):
        run_eval(_plan(judge=_CloudJudge()), tmp_path / "corpus")


def test_a_run_refuses_a_local_judge_declared_routed(tmp_path: Path) -> None:
    """The mismatch is refused in both directions, not only the unsafe one."""
    with pytest.raises(EvalPlanError, match="judge_kind"):
        run_eval(_plan(judge_kind=JudgeKind.ROUTED), tmp_path / "corpus")
