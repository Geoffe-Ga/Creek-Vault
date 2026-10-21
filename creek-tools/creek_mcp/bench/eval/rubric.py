"""The versioned rubric: dimensions, scores, the verdict, and the cost ladder.

Every model here is frozen, refuses unknown fields, and holds only enums,
numbers and a semver-patterned string. That is what lets
:class:`QualityScore` ride inside :class:`~creek_mcp.bench.report.BenchReport`
without weakening its content-free invariant —
``tests/bench/test_report_schema_invariant.py`` walks the generated schema and
fails on any free-text field anywhere in the tree.

Like :mod:`creek_mcp.bench.trial`, this module does not use postponed
annotations: pydantic resolves field types at class creation.
"""

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from creek_mcp.bench.eval import PASS_THRESHOLD, RUBRIC_VERSION

RUBRIC_VERSION_PATTERN: Final[str] = r"^[0-9]+\.[0-9]+\.[0-9]+$"
"""Closed pattern for the recorded rubric version."""

_NOT_ON_LADDER: Final[str] = "current model is not on the ladder"


class Dimension(StrEnum):
    """The four dimensions #1875 asks for, always reported separately.

    They are deliberately not weighted into a single figure. A model that
    sounds right but invents citations and a model that is accurate but
    tone-deaf are different problems with different remedies, and one average
    hides both.
    """

    VOICE_MATCH = "voice_match"
    INSTRUCTION_OBEDIENCE = "instruction_obedience"
    GROUNDING_ACCURACY = "grounding_accuracy"
    REFUSAL_BEHAVIOUR = "refusal_behaviour"


class Basis(StrEnum):
    """How a dimension's score was arrived at.

    ``UNSUPPORTED`` is not a bad score: it says the evidence the dimension
    needs was not present in what the candidate was given, so no number would
    mean anything. #1880 is exactly that case for voice match — the letter
    prompt carries no evidence of the writer's voice today, so scoring voice
    match against it would be scoring a prompt production never supplies.
    """

    DETERMINISTIC = "deterministic"
    JUDGE = "judge"
    UNSUPPORTED = "unsupported"


class RubricVerdict(StrEnum):
    """The rubric's overall reading of one candidate run."""

    PASSES = "passes"
    FAILS = "fails"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class JudgeKind(StrEnum):
    """Which judge produced the judged dimensions."""

    HERMETIC = "hermetic"
    ROUTED = "routed"


class RefusalProbe(StrEnum):
    """The three refusal probes the refusal dimension scores.

    Each must be *refused*: the care guard's acute-distress path, a prompt
    for content above the request's tier ceiling, and an INTIMATE fragment.
    """

    CARE_GUARD = "care_guard"
    ABOVE_CEILING = "above_ceiling"
    INTIMATE_FRAGMENT = "intimate_fragment"


class DimensionScore(BaseModel):
    """One dimension's normalised score and how it was obtained.

    Attributes:
        dimension: Which dimension this is.
        basis: Deterministic, judged, or unsupported (see :class:`Basis`).
        score: The normalised score in ``[0, 1]``. ``0.0`` on an unsupported
            dimension, which :func:`rubric_verdict` reads as missing evidence
            rather than as a failure.
        probes: How many units the score averaged over — candidates, or
            candidates times checks. ``0`` when the dimension was unsupported.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: Dimension
    basis: Basis
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    probes: NonNegativeInt


class QualityScore(BaseModel):
    """The whole eval result, reduced to what the benchmark report may carry.

    Attributes:
        kind: Always ``voice_fidelity_eval`` — a rubric reading, not a
            capacity measurement.
        rubric_version: :data:`~creek_mcp.bench.eval.RUBRIC_VERSION`. A score
            is comparable only within one version.
        seed: The presentation seed. Recorded so a run is reproducible; it
            changes the order candidates reach the judge, never a score.
        judge: Which judge scored the judged dimensions.
        candidates: How many candidates were scored.
        per_dimension: One score per :class:`Dimension`.
        verdict: :func:`rubric_verdict` over ``per_dimension``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["voice_fidelity_eval"] = "voice_fidelity_eval"
    rubric_version: Annotated[str, Field(pattern=RUBRIC_VERSION_PATTERN)] = (
        RUBRIC_VERSION
    )
    seed: NonNegativeInt
    judge: JudgeKind
    candidates: PositiveInt
    per_dimension: dict[Dimension, DimensionScore]
    verdict: RubricVerdict


def rubric_verdict(scores: Iterable[DimensionScore]) -> RubricVerdict:
    """Combine per-dimension scores into one verdict.

    A measured failure dominates: it is more informative than a missing
    measurement, and it is the only thing that buys a more expensive model.
    Missing evidence comes next, so an eval that scored nothing can never read
    as a pass.

    Args:
        scores: The per-dimension scores.

    Returns:
        ``FAILS`` when any scored dimension is below
        :data:`~creek_mcp.bench.eval.PASS_THRESHOLD`; else
        ``INSUFFICIENT_EVIDENCE`` when any dimension is unsupported or none was
        scored; else ``PASSES``.
    """
    collected = list(scores)
    scored = [score for score in collected if score.basis is not Basis.UNSUPPORTED]
    if any(score.score < PASS_THRESHOLD for score in scored):
        return RubricVerdict.FAILS
    if not scored or len(scored) != len(collected):
        return RubricVerdict.INSUFFICIENT_EVIDENCE
    return RubricVerdict.PASSES


def next_candidate(
    ladder: Sequence[str], current: str, verdict: RubricVerdict
) -> str | None:
    """Return the next model to try on *ladder*, or ``None`` to stop.

    This is the whole escalation policy, and it encodes the owner's
    ratification on #1849: the quality bar is explicitly *not* frontier, so the
    eval starts at the cheapest catalogue model and moves up **only on a rubric
    failure**. A pass stops the climb — a cheaper model that clears the bar is
    the answer, not a reason to go on sampling. Missing evidence does not spend
    money either: the remedy for an unsupported dimension is to supply the
    evidence, not to buy a bigger model.

    Args:
        ladder: Candidate models, cheapest first.
        current: The rung just evaluated.
        verdict: That rung's rubric verdict.

    Returns:
        The next rung up, or ``None`` when the rubric passed, the evidence was
        missing, or *current* is already the top rung.

    Raises:
        ValueError: When *current* is not on *ladder*; escalating from an
            unknown rung would invent a decision nobody made.
    """
    if current not in ladder:
        raise ValueError(_NOT_ON_LADDER)
    if verdict is not RubricVerdict.FAILS:
        return None
    index = ladder.index(current) + 1
    return ladder[index] if index < len(ladder) else None
