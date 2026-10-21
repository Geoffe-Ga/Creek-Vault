"""The rubric: versioned dimensions, verdict combination, and the cost ladder.

The ladder encodes the owner's ratification on #1849 — the quality bar is
*explicitly not frontier*, so the eval starts at the cheapest catalogue model
and climbs only when the rubric actually fails. A rubric that passes stops the
ladder; "bigger is better" is not expressible here.
"""

from __future__ import annotations

import pytest

from creek_mcp.bench.eval import PASS_THRESHOLD, RUBRIC_VERSION
from creek_mcp.bench.eval.rubric import (
    Basis,
    Dimension,
    DimensionScore,
    JudgeKind,
    QualityScore,
    RubricVerdict,
    next_candidate,
    rubric_verdict,
)

_LADDER = ("cheap-small", "mid", "frontier")


def _score(
    dimension: Dimension, score: float, basis: Basis = Basis.JUDGE
) -> DimensionScore:
    """Build one dimension score."""
    return DimensionScore(dimension=dimension, basis=basis, score=score, probes=1)


def test_rubric_version_is_semver() -> None:
    """The version is recorded in every artifact, so it must be pinnable."""
    assert RUBRIC_VERSION.count(".") == 2


def test_all_four_dimensions_exist() -> None:
    """#1875 names exactly four dimensions; they are reported separately."""
    assert {d.value for d in Dimension} == {
        "voice_match",
        "instruction_obedience",
        "grounding_accuracy",
        "refusal_behaviour",
    }


def test_verdict_passes_when_every_dimension_clears_the_threshold() -> None:
    """All scored and above threshold is the only passing shape."""
    scores = [_score(d, PASS_THRESHOLD) for d in Dimension]
    assert rubric_verdict(scores) is RubricVerdict.PASSES


def test_verdict_fails_on_any_dimension_below_the_threshold() -> None:
    """One failing dimension fails the rubric, whatever the others say."""
    scores = [_score(d, 1.0) for d in Dimension]
    scores[0] = _score(Dimension.VOICE_MATCH, PASS_THRESHOLD - 0.01)
    assert rubric_verdict(scores) is RubricVerdict.FAILS


def test_verdict_is_insufficient_evidence_when_a_dimension_is_unsupported() -> None:
    """An unsupported dimension is not a pass and not a failure (#1880)."""
    scores = [_score(d, 1.0) for d in Dimension]
    scores[0] = _score(Dimension.VOICE_MATCH, 0.0, Basis.UNSUPPORTED)
    assert rubric_verdict(scores) is RubricVerdict.INSUFFICIENT_EVIDENCE


def test_a_failure_outranks_unsupported_evidence() -> None:
    """A measured failure is more informative than a missing measurement."""
    scores = [
        _score(Dimension.VOICE_MATCH, 0.0, Basis.UNSUPPORTED),
        _score(Dimension.GROUNDING_ACCURACY, 0.0),
    ]
    assert rubric_verdict(scores) is RubricVerdict.FAILS


def test_no_scores_is_insufficient_evidence() -> None:
    """An empty eval asserts nothing and must not read as a pass."""
    assert rubric_verdict([]) is RubricVerdict.INSUFFICIENT_EVIDENCE


def test_ladder_stops_at_the_cheapest_model_that_passes() -> None:
    """A pass is the end of the climb — the owner's bar is not frontier."""
    assert next_candidate(_LADDER, "cheap-small", RubricVerdict.PASSES) is None


def test_ladder_climbs_one_rung_on_a_rubric_failure() -> None:
    """Only a failure buys a more expensive model, and only the next one."""
    assert next_candidate(_LADDER, "cheap-small", RubricVerdict.FAILS) == "mid"


def test_ladder_climbs_on_insufficient_evidence_only_when_told_to() -> None:
    """Missing evidence is not a failure, so it does not spend more money."""
    assert (
        next_candidate(_LADDER, "cheap-small", RubricVerdict.INSUFFICIENT_EVIDENCE)
        is None
    )


def test_ladder_exhausted_at_the_top() -> None:
    """A failure at the top of the ladder has nowhere to climb."""
    assert next_candidate(_LADDER, "frontier", RubricVerdict.FAILS) is None


def test_ladder_refuses_a_model_it_does_not_contain() -> None:
    """Escalating from an unknown rung would invent a decision."""
    with pytest.raises(ValueError, match="ladder"):
        next_candidate(_LADDER, "unlisted", RubricVerdict.FAILS)


def test_quality_score_is_frozen_and_closed() -> None:
    """The score rides in the shareable report, so it is closed by construction."""
    quality = QualityScore(
        rubric_version=RUBRIC_VERSION,
        seed=7,
        judge=JudgeKind.HERMETIC,
        candidates=1,
        per_dimension={d: _score(d, 1.0) for d in Dimension},
        verdict=RubricVerdict.PASSES,
    )
    assert quality.model_config["frozen"] is True
    assert quality.model_config["extra"] == "forbid"
    assert quality.kind == "voice_fidelity_eval"


def test_dimension_score_refuses_a_score_outside_the_unit_interval() -> None:
    """Scores are normalised, so an out-of-range value is a bug not a reading."""
    with pytest.raises(ValueError, match="less than or equal to 1"):
        _score(Dimension.VOICE_MATCH, 1.5)
